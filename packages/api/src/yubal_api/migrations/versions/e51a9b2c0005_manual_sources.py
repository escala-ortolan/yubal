"""Owner-registered local arrivals share the verified-source ledger, not downloads.

SQLite VARCHAR lengths are not enforced: existing ID columns have TEXT affinity
and need no destructive table copy to hold namespaced local source identities.
Public YouTube submission IDs remain strictly 11 characters.
"""

import sqlalchemy as sa
from alembic import op

revision = "e51a9b2c0005"
down_revision = "d42b6c1a0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "manual_sources",
        sa.Column(
            "source_id",
            sa.String(64),
            sa.ForeignKey("source_tracks.video_id"),
            primary_key=True,
        ),
        sa.Column("original_path", sa.String(4096), nullable=False),
        sa.Column("original_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM manual_sources"))
        .scalar_one()
    ):
        raise RuntimeError(
            "Manual records exist; restore a pre-registration backup for rollback"
        )
    op.drop_table("manual_sources")
