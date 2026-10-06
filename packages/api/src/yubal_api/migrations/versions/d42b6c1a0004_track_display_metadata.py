"""Persist downloader-provided track title and artist for the dashboard."""

import re
from pathlib import PurePosixPath

import sqlalchemy as sa
from alembic import op

revision = "d42b6c1a0004"
down_revision = "91a30c1e0003"
branch_labels = None
depends_on = None


def _legacy_name(path: str | None) -> tuple[str | None, str | None]:
    if not path:
        return None, None
    stem = PurePosixPath(path).name.removesuffix(".m4a")
    stem = re.sub(r" \[[A-Za-z0-9_-]{11}(?:-[0-9a-f]{8})?\]$", "", stem)
    if " - " not in stem:
        return stem or None, None
    artist, title = stem.split(" - ", 1)
    return (title or None, artist or None)


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("source_tracks")
    }
    if "title" not in columns:
        op.add_column("source_tracks", sa.Column("title", sa.String(500)))
    if "artist" not in columns:
        op.add_column("source_tracks", sa.Column("artist", sa.String(500)))
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT video_id, audio_path FROM source_tracks "
            "WHERE audio_path IS NOT NULL"
        )
    ).all()
    for video_id, path in rows:
        title, artist = _legacy_name(path)
        connection.execute(
            sa.text(
                "UPDATE source_tracks SET title=:title, artist=:artist "
                "WHERE video_id=:video_id"
            ),
            {"title": title, "artist": artist, "video_id": video_id},
        )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("source_tracks")
    }
    if "artist" in columns:
        op.drop_column("source_tracks", "artist")
    if "title" in columns:
        op.drop_column("source_tracks", "title")
