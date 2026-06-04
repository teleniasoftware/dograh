from types import SimpleNamespace

import pytest

from api.schemas.user_configuration import UserConfiguration
from api.services.configuration.registry import ServiceProviders
from api.tasks import knowledge_base_processing


def test_user_configuration_accepts_openai_compatible_embeddings():
    config = UserConfiguration.model_validate(
        {
            "embeddings": {
                "provider": ServiceProviders.OPENROUTER.value,
                "model": "text-embedding-3-small",
                "base_url": "http://localhost:11434/v1",
                "api_key": "test-key",
            }
        }
    )

    assert config.embeddings is not None
    assert config.embeddings.provider == ServiceProviders.OPENROUTER
    assert config.embeddings.base_url == "http://localhost:11434/v1"


@pytest.mark.asyncio
async def test_process_knowledge_base_document_uses_local_processing(monkeypatch):
    statuses = []
    created_chunks = []
    document = SimpleNamespace(id=10, created_by=20)
    user_config = UserConfiguration.model_validate(
        {
            "embeddings": {
                "provider": ServiceProviders.OPENROUTER.value,
                "model": "text-embedding-3-small",
                "base_url": "http://localhost:11434/v1",
                "api_key": "test-key",
            }
        }
    )

    class FakeDBClient:
        async def update_document_status(self, document_id, status, **kwargs):
            statuses.append((document_id, status, kwargs))

        async def update_document_metadata(self, document_id, **kwargs):
            return None

        async def get_document_by_id(self, document_id):
            return document

        async def get_document_by_hash(self, file_hash, organization_id):
            return None

        def compute_file_hash(self, file_path):
            return "hash"

        def get_mime_type(self, file_path):
            return "text/plain"

        async def get_user_configurations(self, user_id):
            assert user_id == document.created_by
            return user_config

        async def create_chunks_batch(self, chunks):
            created_chunks.extend(chunks)
            return chunks

    class FakeStorage:
        async def adownload_file(self, s3_key, destination_path):
            with open(destination_path, "w", encoding="utf-8") as file:
                file.write("alpha beta gamma delta epsilon zeta eta theta")
            return True

    class FakeEmbeddingService:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "test-key"
            assert kwargs["base_url"] == "http://localhost:11434/v1"
            assert kwargs["model_id"] == "text-embedding-3-small"

        def get_model_id(self):
            return "text-embedding-3-small"

        def get_embedding_dimension(self):
            return 1536

        async def embed_texts(self, texts):
            return [[0.01] * 1536 for _ in texts]

    async def fake_contextualize_chunks(chunks, full_text, llm_config):
        return [None for _ in chunks]

    monkeypatch.setattr(knowledge_base_processing, "db_client", FakeDBClient())
    monkeypatch.setattr(knowledge_base_processing, "storage_fs", FakeStorage())
    monkeypatch.setattr(
        knowledge_base_processing,
        "OpenAIEmbeddingService",
        FakeEmbeddingService,
    )
    monkeypatch.setattr(
        knowledge_base_processing,
        "_contextualize_chunks",
        fake_contextualize_chunks,
    )

    await knowledge_base_processing.process_knowledge_base_document(
        {},
        document_id=10,
        s3_key="knowledge_base/1/doc/test.txt",
        organization_id=1,
        created_by_provider_id="provider",
        max_tokens=4,
        retrieval_mode="chunked",
    )

    assert statuses[0][1] == "processing"
    assert statuses[-1][1] == "completed"
    assert statuses[-1][2]["total_chunks"] == len(created_chunks)
    assert len(created_chunks) > 0
    assert created_chunks[0].embedding_model == "text-embedding-3-small"
