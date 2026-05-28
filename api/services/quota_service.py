"""Quota checking hooks.

External Dograh credit checks have been removed. The public functions remain so
call paths that expect a quota result can proceed without contacting external
credit services.
"""

from dataclasses import dataclass

from api.db import db_client
from api.db.models import UserModel


@dataclass
class QuotaCheckResult:
    """Result of a quota check."""

    has_quota: bool
    error_message: str = ""
    error_code: str = ""


async def check_dograh_quota(
    user: UserModel, workflow_id: int | None = None
) -> QuotaCheckResult:
    """Return success without contacting external credit services."""
    return QuotaCheckResult(has_quota=True)


async def check_dograh_quota_by_user_id(
    user_id: int, workflow_id: int | None = None
) -> QuotaCheckResult:
    """Check Dograh quota by user ID.

    Convenience function that fetches the user and then checks quota. When
    ``workflow_id`` is provided, the workflow's ``model_overrides`` are
    applied so the quota check evaluates the credentials that will actually
    be used for the call.

    Args:
        user_id: The ID of the user to check quota for
        workflow_id: Optional workflow whose per-workflow overrides should
            be applied to the user's config before checking quota.

    Returns:
        QuotaCheckResult with quota status
    """
    user = await db_client.get_user_by_id(user_id)
    if not user:
        return QuotaCheckResult(
            has_quota=False,
            error_message="User not found",
        )
    return await check_dograh_quota(user, workflow_id=workflow_id)
