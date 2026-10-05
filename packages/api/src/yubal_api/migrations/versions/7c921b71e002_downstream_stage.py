"""Keep tagging/filing status distinct from verified audio.

Revision ID: 7c921b71e002
Revises: 51c7acb10a01
"""

from collections.abc import Sequence

from alembic import op
from yubal_api.db.intake_ledger import downstream_stages, metadata

revision: str = "7c921b71e002"
down_revision: str | Sequence[str] | None = "51c7acb10a01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind(), tables=[downstream_stages])


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), tables=[downstream_stages])
