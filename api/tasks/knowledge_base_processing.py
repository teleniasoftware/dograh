"""ARQ background task for processing knowledge base documents."""

import os
import tempfile
from typing import Any

from loguru import logger
from openai import AsyncOpenAI

from api.db import db_client
from api.db.models import KnowledgeBaseChunkModel
from api.services.gen_ai import OpenAIEmbeddingService
from api.services.storage import storage_fs
from api.utils.url_security import validate_user_configured_service_url

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024
CHUNK_OVERLAP_TOKENS = 32
CONTEXTUALIZATION_TIMEOUT_SECONDS = 20.0


def _extract_text_from_file(file_path: str) -> tuple[str, dict[str, Any]]:
    """Extract text from locally downloaded files without external services."""
    with open(file_path, "rb") as file:
        raw_content = file.read()

    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            text = raw_content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw_content.decode("utf-8", errors="ignore")

    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    metadata = {
        "processor": "local",
        "extraction": "direct_text_decode",
        "characters": len(text),
    }
    return text, metadata


def _chunk_text(text: str, max_tokens: int) -> list[dict[str, Any]]:
    """Split extracted text using a deterministic word-token approximation."""
    words = text.split()
    if not words:
        return []

    chunk_size = max(max_tokens, 1)
    overlap = min(CHUNK_OVERLAP_TOKENS, max(chunk_size - 1, 0))
    step = max(chunk_size - overlap, 1)

    chunks = []
    for chunk_index, start in enumerate(range(0, len(words), step)):
        end = min(start + chunk_size, len(words))
        chunk_words = words[start:end]
        chunks.append(
            {
                "text": " ".join(chunk_words),
                "index": chunk_index,
                "metadata": {
                    "start_token": start,
                    "end_token": end,
                    "tokenizer": "word_approximation",
                },
                "token_count": len(chunk_words),
            }
        )
        if end == len(words):
            break

    return chunks


async def _contextualize_chunks(
    chunks: list[dict[str, Any]],
    full_text: str,
    llm_config: Any,
) -> list[str | None]:
    """Use the configured local LLM to add retrieval context to each chunk."""
    if not llm_config or not getattr(llm_config, "base_url", None):
        return [None for _ in chunks]

    base_url = llm_config.base_url
    validate_user_configured_service_url(base_url, field_name="base_url")
    api_key = getattr(llm_config, "api_key", None) or "none"
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    document_preview = full_text[:4000]
    contextualized_chunks: list[str | None] = []

    for chunk in chunks:
        try:
            response = await client.chat.completions.create(
                model=llm_config.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Add concise document context to the provided chunk for "
                            "retrieval. Return only the contextualized chunk text."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Document preview:\n{document_preview}\n\n"
                            f"Chunk:\n{chunk['text']}"
                        ),
                    },
                ],
                timeout=CONTEXTUALIZATION_TIMEOUT_SECONDS,
            )
            content = response.choices[0].message.content
            contextualized_chunks.append(content.strip() if content else None)
        except Exception as exc:
            logger.warning(
                "Knowledge base chunk contextualization failed for chunk "
                f"{chunk['index']}: {exc}"
            )
            contextualized_chunks.append(None)

    return contextualized_chunks


async def process_knowledge_base_document(
    ctx,
    document_id: int,
    s3_key: str,
    organization_id: int,
    created_by_provider_id: str,
    max_tokens: int = 128,
    retrieval_mode: str = "chunked",
):
    """Process a knowledge base document.

    Args:
        ctx: ARQ context
        document_id: Database ID of the document
        s3_key: S3 key where the file is stored
        organization_id: Organization ID
        created_by_provider_id: Uploading user's provider ID
        max_tokens: Maximum number of tokens per chunk (default: 128)
        retrieval_mode: "chunked" for vector search or "full_document" for full text
    """
    logger.info(
        f"Processing knowledge base document: document_id={document_id}, "
        f"s3_key={s3_key}, org={organization_id}, mode={retrieval_mode}"
    )

    temp_file_path = None

    try:
        await db_client.update_document_status(document_id, "processing")

        filename = s3_key.split("/")[-1]
        file_extension = os.path.splitext(filename)[1] or ".bin"

        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=file_extension)
        temp_file_path = temp_file.name
        temp_file.close()

        logger.info(f"Downloading file from S3: {s3_key}")
        download_success = await storage_fs.adownload_file(s3_key, temp_file_path)
        if not download_success:
            raise Exception(f"Failed to download file from S3: {s3_key}")
        if not os.path.exists(temp_file_path):
            raise FileNotFoundError(f"Downloaded file not found: {temp_file_path}")

        file_size = os.path.getsize(temp_file_path)
        logger.info(f"Downloaded file size: {file_size} bytes")

        if file_size > MAX_FILE_SIZE_BYTES:
            error_message = (
                f"File size ({file_size / (1024 * 1024):.1f}MB) exceeds the "
                f"maximum allowed size of {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB."
            )
            logger.warning(f"Document {document_id}: {error_message}")
            await db_client.update_document_status(
                document_id, "failed", error_message=error_message
            )
            return

        file_hash = db_client.compute_file_hash(temp_file_path)
        mime_type = db_client.get_mime_type(temp_file_path)

        document = await db_client.get_document_by_id(document_id)
        if not document:
            raise Exception(f"Document {document_id} not found")

        # Reject duplicates (same hash already ingested for this org).
        existing_doc = await db_client.get_document_by_hash(file_hash, organization_id)
        if existing_doc and existing_doc.id != document_id:
            error_message = (
                f"This file is a duplicate of '{existing_doc.filename}'. "
                f"Please delete the duplicate files and consolidate them into a "
                f"single unique file before uploading."
            )
            logger.warning(
                f"Duplicate document detected: {document_id} is duplicate of "
                f"{existing_doc.id} ({existing_doc.filename})"
            )
            await db_client.update_document_metadata(
                document_id,
                file_size_bytes=file_size,
                file_hash=file_hash,
                mime_type=mime_type,
            )
            await db_client.update_document_status(
                document_id,
                "failed",
                error_message=error_message,
                docling_metadata={
                    "duplicate_of": existing_doc.document_uuid,
                    "duplicate_filename": existing_doc.filename,
                },
            )
            return

        await db_client.update_document_metadata(
            document_id,
            file_size_bytes=file_size,
            file_hash=file_hash,
            mime_type=mime_type,
        )

        full_text, processing_metadata = _extract_text_from_file(temp_file_path)
        if not full_text:
            raise ValueError("No text could be extracted from the uploaded document")

        if retrieval_mode == "full_document":
            await db_client.update_document_full_text(document_id, full_text)
            await db_client.update_document_status(
                document_id,
                "completed",
                total_chunks=1,
                docling_metadata=processing_metadata,
            )
            logger.info(f"Document {document_id} processed in full_document mode")
            return

        user_config = await db_client.get_user_configurations(document.created_by)
        if not user_config.embeddings:
            raise ValueError(
                "Embeddings provider not configured. Configure a local embeddings "
                "model in Model Configurations > Embedding."
            )

        chunks = _chunk_text(full_text, max_tokens=max_tokens)
        if not chunks:
            raise ValueError("No chunks could be created from the uploaded document")

        contextualized_texts = await _contextualize_chunks(
            chunks,
            full_text,
            user_config.llm,
        )

        embedding_service = OpenAIEmbeddingService(
            db_client=db_client,
            api_key=user_config.embeddings.api_key,
            model_id=user_config.embeddings.model,
            base_url=getattr(user_config.embeddings, "base_url", None),
        )
        texts_to_embed = [
            contextualized or chunk["text"]
            for chunk, contextualized in zip(chunks, contextualized_texts)
        ]
        embeddings = await embedding_service.embed_texts(texts_to_embed)
        expected_dimension = embedding_service.get_embedding_dimension()
        for embedding in embeddings:
            if len(embedding) != expected_dimension:
                raise ValueError(
                    f"Embedding model '{embedding_service.get_model_id()}' returned "
                    f"{len(embedding)} dimensions, but the knowledge base vector "
                    f"store expects {expected_dimension}."
                )

        chunk_models = [
            KnowledgeBaseChunkModel(
                document_id=document_id,
                organization_id=organization_id,
                chunk_text=chunk["text"],
                contextualized_text=contextualized,
                chunk_index=chunk["index"],
                chunk_metadata=chunk["metadata"],
                embedding_model=embedding_service.get_model_id(),
                embedding_dimension=len(embedding),
                embedding=embedding,
                token_count=chunk["token_count"],
            )
            for chunk, contextualized, embedding in zip(
                chunks, contextualized_texts, embeddings
            )
        ]

        await db_client.create_chunks_batch(chunk_models)
        await db_client.update_document_status(
            document_id,
            "completed",
            total_chunks=len(chunk_models),
            docling_metadata={
                **processing_metadata,
                "chunk_count": len(chunk_models),
                "embedding_model": embedding_service.get_model_id(),
                "llm_model": getattr(user_config.llm, "model", None),
            },
        )
        logger.info(f"Document {document_id} processed with {len(chunk_models)} chunks")

    except Exception as e:
        logger.error(
            f"Error processing knowledge base document {document_id}: {e}",
            exc_info=True,
        )
        await db_client.update_document_status(
            document_id, "failed", error_message=str(e)
        )
        raise

    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                logger.debug(f"Cleaned up temp file: {temp_file_path}")
            except Exception as e:
                logger.warning(f"Failed to clean up temp file {temp_file_path}: {e}")
