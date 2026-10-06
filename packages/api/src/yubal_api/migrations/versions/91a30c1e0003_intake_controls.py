"""Durable intake controls, reset receipts and playlist schedules."""

import sqlalchemy as sa
from alembic import op

revision = "91a30c1e0003"
down_revision = "7c921b71e002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intake_controls",
        sa.Column(
            "intake_id", sa.String(36), sa.ForeignKey("intakes.id"), primary_key=True
        ),
        sa.Column("state", sa.String(16), nullable=False),
    )
    op.create_table(
        "intake_actions",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("video_id", sa.String(11), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("result", sa.Text, nullable=False),
        sa.Column("previous", sa.Text, nullable=False),
    )
    op.create_table(
        "intake_schedules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("playlist_id", sa.String(256), nullable=False),
        sa.Column("cron", sa.String(100), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("limit", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("next_run", sa.Float, nullable=False),
        sa.Column("run_id", sa.String(36)),
        sa.Column("pending_payload", sa.Text),
        sa.Column("last_run", sa.Float),
        sa.Column("last_intake_id", sa.String(36)),
        sa.Column("last_error", sa.String(256)),
    )
    op.create_index("ix_intake_schedules_device_id", "intake_schedules", ["device_id"])


def downgrade() -> None:
    op.drop_table("intake_schedules")
    op.drop_table("intake_actions")
    op.drop_table("intake_controls")
