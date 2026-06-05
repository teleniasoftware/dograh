"""merge email case insensitive and tvox callback heads

Revision ID: a0f7db36446c
Revises: 384be6596b36, 8f3d2a1b6c4e
Create Date: 2026-06-05 08:36:56.230492

"""

from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = "a0f7db36446c"
down_revision: Union[str, Sequence[str], None] = (
    "384be6596b36",
    "8f3d2a1b6c4e",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
