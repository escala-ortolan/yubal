"""Add durable intake identity and download attempts.

Revision ID: 51c7acb10a01
Revises: 03132d5514f9
"""

from collections.abc import Sequence

from alembic import op
from yubal_api.db.intake_ledger import metadata

revision: str = "51c7acb10a01"
down_revision: str | Sequence[str] | None = "03132d5514f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNED_TABLES = [
    metadata.tables[name]
    for name in (
        "source_tracks",
        "intakes",
        "intake_items",
        "source_aliases",
        "download_attempts",
        "intake_devices",
        "intake_rate_events",
    )
]


def upgrade() -> None:
    """Create identity tables, leaving existing subscriptions untouched."""
    metadata.create_all(bind=op.get_bind(), tables=OWNED_TABLES)


def downgrade() -> None:
    """Drop only the tables owned by this migration."""
    metadata.drop_all(bind=op.get_bind(), tables=OWNED_TABLES)
