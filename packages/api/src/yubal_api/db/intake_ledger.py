"""Durable source identity and ordered intake intent (no download orchestration)."""

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import Connection, Engine, MetaData, text

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
metadata = MetaData()

# Verified audio, not a URL or filename, is what marks a source downloaded.
SourceState = Literal[
    "pending", "downloading", "downloaded", "failed", "missing_output"
]
# Tagging and filing are tracked apart from the download that fed them.
DownstreamState = Literal[
    "not_started", "waiting_for_tagger", "tagged", "filed", "missing_output"
]

source_tracks = sa.Table(
    "source_tracks",
    metadata,
    sa.Column("video_id", sa.String(11), primary_key=True),
    sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
    sa.Column("lease_token", sa.String(36)),
    sa.Column("audio_path", sa.String(4096)),
    sa.Column("audio_sha256", sa.String(64)),
    sa.Column("pcm_sha256", sa.String(64)),
)
intakes = sa.Table(
    "intakes",
    metadata,
    sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("id", sa.String(36), nullable=False, unique=True),
    sa.Column("request_id", sa.String(36), nullable=False, unique=True),
    sa.Column("device_id", sa.String(128), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
)
intake_controls = sa.Table(
    "intake_controls",
    metadata,
    sa.Column(
        "intake_id", sa.String(36), sa.ForeignKey("intakes.id"), primary_key=True
    ),
    sa.Column("state", sa.String(16), nullable=False),
)
intake_items = sa.Table(
    "intake_items",
    metadata,
    sa.Column(
        "intake_id", sa.String(36), sa.ForeignKey("intakes.id"), primary_key=True
    ),
    sa.Column("position", sa.Integer(), primary_key=True),
    sa.Column(
        "video_id",
        sa.String(11),
        sa.ForeignKey("source_tracks.video_id"),
        nullable=False,
    ),
)
aliases = sa.Table(
    "source_aliases",
    metadata,
    sa.Column("alias_video_id", sa.String(11), primary_key=True),
    sa.Column(
        "canonical_video_id",
        sa.String(11),
        sa.ForeignKey("source_tracks.video_id"),
        nullable=False,
    ),
    sa.Column("evidence", sa.Text(), nullable=False),
)
attempts = sa.Table(
    "download_attempts",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column(
        "video_id",
        sa.String(11),
        sa.ForeignKey("source_tracks.video_id"),
        nullable=False,
    ),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("audio_path", sa.String(4096)),
    sa.Column("audio_sha256", sa.String(64)),
    sa.Column("pcm_sha256", sa.String(64)),
    sa.Column("audio_bytes", sa.BigInteger()),
    sa.Column("duration_seconds", sa.Float()),
    sa.Column("codec", sa.String(32)),
    sa.Column("error", sa.Text()),
    sa.Column("planned_path", sa.String(4096)),
)
devices = sa.Table(
    "intake_devices",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("token_sha256", sa.String(64), nullable=False, unique=True),
    sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
)
rate_events = sa.Table(
    "intake_rate_events",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("device_id", sa.String(128), nullable=False, index=True),
    sa.Column("created_at", sa.Float(), nullable=False),
)
downstream_stages = sa.Table(
    "downstream_stages",
    metadata,
    sa.Column(
        "video_id",
        sa.String(11),
        sa.ForeignKey("source_tracks.video_id"),
        primary_key=True,
    ),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("tagging_evidence", sa.Text()),
    sa.Column("planned_final_path", sa.String(4096)),
    sa.Column("planned_sidecars", sa.Text()),
    sa.Column("final_path", sa.String(4096)),
    sa.Column("error", sa.Text()),
)


class IntakeConflict(ValueError):
    """A request ID or alias already belongs to different intent."""


class IntakeCapacityError(ValueError):
    """A device has too many pending or active distinct sources."""


class IntakeRateLimitError(ValueError):
    """A device exceeded its new-intake submission rate."""


class FilingRefused(ValueError):
    """Filing would contradict, overwrite or abandon recorded downstream state."""


@dataclass(frozen=True)
class Claim:
    video_id: str
    attempt_id: str


@dataclass(frozen=True)
class VerifiedAudio:
    path: str
    size: int
    sha256: str
    duration_seconds: float
    codec: str
    pcm_sha256: str | None = None


@dataclass(frozen=True)
class FilingCandidate:
    """A verified source whose recorded audio path is known and must still exist."""

    video_id: str
    status: str
    source_path: Path
    pcm_sha256: str
    destination: Path
    sidecars: list[tuple[str, str]]


class IntakeLedger:
    """Local SQLite ledger. Only a verified worker may close download attempts."""

    MAX_PENDING_PER_DEVICE = 500
    MAX_NEW_INTAKES_PER_MINUTE = 20

    def __init__(self, engine: Engine, clock: Callable[[], float] = time.time) -> None:
        if engine.dialect.name != "sqlite":
            raise ValueError("IntakeLedger currently requires SQLite")
        self.engine = engine
        self.clock = clock

    @staticmethod
    def _video_id(video_id: str) -> str:
        if not VIDEO_ID.fullmatch(video_id):
            raise ValueError("Invalid YouTube video ID")
        return video_id

    def submit(
        self,
        request_id: str,
        device_id: str,
        mode: str,
        video_ids: list[str],
        *,
        details: dict[str, object] | None = None,
    ) -> str:
        request_id = str(UUID(request_id))
        if not device_id or len(device_id) > 128:
            raise ValueError("Invalid device ID")
        if mode not in {
            "manual_song",
            "manual_queue",
            "manual_playlist",
            "auto_song",
            "auto_queue",
            "auto_playlist",
        }:
            raise ValueError("Invalid intake mode")
        if not video_ids or len(video_ids) > 100:
            raise ValueError("Intake must contain 1-100 tracks")
        video_ids = [self._video_id(video_id) for video_id in video_ids]
        payload = json.dumps(
            [device_id, mode, video_ids, details],
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            existing = (
                connection.execute(
                    text(
                        "SELECT id, payload FROM intakes WHERE request_id = :request_id"
                    ),
                    {"request_id": request_id},
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["payload"] != payload:
                    raise IntakeConflict(
                        "Request ID already used with different intent"
                    )
                return str(existing["id"])
            now = self.clock()
            connection.execute(
                rate_events.delete().where(rate_events.c.created_at < now - 3600)
            )
            recent = connection.execute(
                sa.select(sa.func.count())
                .select_from(rate_events)
                .where(
                    rate_events.c.device_id == device_id,
                    rate_events.c.created_at > now - 60,
                )
            ).scalar_one()
            if recent >= self.MAX_NEW_INTAKES_PER_MINUTE:
                raise IntakeRateLimitError("Device submission rate exceeded")
            active = set(
                connection.execute(
                    text(
                        "SELECT DISTINCT i.video_id FROM intake_items i "
                        "JOIN intakes t ON t.id = i.intake_id "
                        "JOIN source_tracks s ON s.video_id = i.video_id "
                        "WHERE t.device_id = :device AND s.state IN "
                        "('pending', 'downloading')"
                    ),
                    {"device": device_id},
                ).scalars()
            )
            states = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    sa.select(source_tracks.c.video_id, source_tracks.c.state).where(
                        source_tracks.c.video_id.in_(video_ids)
                    )
                )
            }
            incoming = {
                video_id
                for video_id in video_ids
                if states.get(video_id, "pending") in {"pending", "downloading"}
            }
            if len(active | incoming) > self.MAX_PENDING_PER_DEVICE:
                raise IntakeCapacityError("Device pending queue is full")
            intake_id = str(uuid4())
            connection.execute(
                intakes.insert().values(
                    id=intake_id,
                    request_id=request_id,
                    device_id=device_id,
                    payload=payload,
                )
            )
            connection.execute(
                rate_events.insert().values(device_id=device_id, created_at=now)
            )
            for position, video_id in enumerate(video_ids):
                connection.execute(
                    text("INSERT OR IGNORE INTO source_tracks(video_id) VALUES (:id)"),
                    {"id": video_id},
                )
                connection.execute(
                    intake_items.insert().values(
                        intake_id=intake_id, position=position, video_id=video_id
                    )
                )
            return intake_id

    def items(self, intake_id: str) -> list[dict[str, str | int | None]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT i.position, i.video_id, s.state AS status, "
                    "COALESCE(ds.status, 'not_started') AS downstream_status, "
                    "ds.final_path AS final_path "
                    "FROM intake_items i "
                    "LEFT JOIN source_aliases a ON a.alias_video_id = i.video_id "
                    "JOIN source_tracks s ON s.video_id = "
                    "COALESCE(a.canonical_video_id, i.video_id) "
                    "LEFT JOIN downstream_stages ds ON ds.video_id = s.video_id "
                    "WHERE i.intake_id = :id ORDER BY i.position"
                ),
                {"id": intake_id},
            ).mappings()
            return [dict(row) for row in rows]

    def intake_for_device(self, intake_id: str, device_id: str) -> bool:
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    text(
                        "SELECT 1 FROM intakes WHERE id = :id AND device_id = :device"
                    ),
                    {"id": intake_id, "device": device_id},
                ).first()
                is not None
            )

    def intake_request(self, intake_id: str) -> dict[str, object]:
        """Original ordered intent; callers must enforce device ownership."""
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(intakes.c.request_id, intakes.c.payload).where(
                    intakes.c.id == intake_id
                )
            ).one()
        device, mode, ids, details = json.loads(row.payload)
        return {
            "request_id": row.request_id,
            **(
                details
                or {
                    "device_id": device,
                    "mode": mode,
                    "tracks": [
                        {"video_id": value, "position": i}
                        for i, value in enumerate(ids)
                    ],
                }
            ),
        }

    def reserve_preview(self, device_id: str) -> None:
        """Charge a metadata lookup against the existing request budget."""
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            now = self.clock()
            connection.execute(
                rate_events.delete().where(rate_events.c.created_at < now - 3600)
            )
            recent = connection.scalar(
                sa.select(sa.func.count())
                .select_from(rate_events)
                .where(
                    rate_events.c.device_id == device_id,
                    rate_events.c.created_at > now - 60,
                )
            )
            if recent is not None and recent >= self.MAX_NEW_INTAKES_PER_MINUTE:
                raise IntakeRateLimitError("Device request rate exceeded")
            connection.execute(
                rate_events.insert().values(device_id=device_id, created_at=now)
            )

    def attempt_details(self, video_id: str) -> list[dict[str, object]]:
        """Allowlisted durable attempt evidence, excluding internal lease data."""
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(
                        attempts.c.id,
                        attempts.c.status,
                        attempts.c.audio_bytes,
                        attempts.c.duration_seconds,
                        attempts.c.codec,
                        attempts.c.error,
                        attempts.c.audio_sha256,
                        attempts.c.pcm_sha256,
                    ).where(attempts.c.video_id == self.resolve(video_id))
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def history(
        self, device_id: str, *, limit: int, before: int | None
    ) -> tuple[list[str], int | None]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT sequence, id FROM intakes WHERE device_id = :device "
                    "AND NOT EXISTS (SELECT 1 FROM intake_controls c "
                    "WHERE c.intake_id = intakes.id AND c.state = 'deleted') "
                    "AND (:before IS NULL OR sequence < :before) "
                    "ORDER BY sequence DESC LIMIT :count"
                ),
                {"device": device_id, "before": before, "count": limit + 1},
            ).all()
            shown = rows[:limit]
            cursor = int(shown[-1][0]) if len(rows) > limit else None
            return [str(row[1]) for row in shown], cursor

    def source_for_device(self, video_id: str, device_id: str) -> bool:
        self._video_id(video_id)
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    text(
                        "SELECT 1 FROM intake_items i "
                        "JOIN intakes t ON t.id = i.intake_id "
                        "WHERE i.video_id = :video_id "
                        "AND t.device_id = :device_id LIMIT 1"
                    ),
                    {"video_id": video_id, "device_id": device_id},
                ).first()
                is not None
            )

    def provision_device(self) -> tuple[str, str]:
        """Local owner operation, never exposed as an unauthenticated HTTP route."""
        device_id, token = str(uuid4()), secrets.token_urlsafe(32)
        with self.engine.begin() as connection:
            connection.execute(
                devices.insert().values(
                    id=device_id,
                    token_sha256=hashlib.sha256(token.encode()).hexdigest(),
                )
            )
        return device_id, token

    def authenticate(self, token: str) -> str | None:
        if not token or len(token) > 256:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.engine.connect() as connection:
            value = connection.execute(
                text(
                    "SELECT id FROM intake_devices WHERE token_sha256 = :digest "
                    "AND revoked = 0"
                ),
                {"digest": digest},
            ).scalar_one_or_none()
            return str(value) if value else None

    def revoke_device(self, device_id: str) -> bool:
        with self.engine.begin() as connection:
            updated = connection.execute(
                text(
                    "UPDATE intake_devices SET revoked = 1 "
                    "WHERE id = :id AND revoked = 0"
                ),
                {"id": device_id},
            )
            return bool(updated.rowcount)

    def source_count(self) -> int:
        with self.engine.connect() as connection:
            return int(
                connection.execute(
                    text("SELECT count(*) FROM source_tracks")
                ).scalar_one()
            )

    def attempt_count(self, video_id: str) -> int:
        with self.engine.connect() as connection:
            return int(
                connection.execute(
                    text("SELECT count(*) FROM download_attempts WHERE video_id = :id"),
                    {"id": self.resolve(video_id)},
                ).scalar_one()
            )

    def resolve(self, video_id: str) -> str:
        self._video_id(video_id)
        with self.engine.connect() as connection:
            return self._resolve(connection, video_id)

    @staticmethod
    def _resolve(connection: Connection, video_id: str) -> str:
        value = connection.execute(
            text(
                "SELECT canonical_video_id FROM source_aliases "
                "WHERE alias_video_id = :id"
            ),
            {"id": video_id},
        ).scalar_one_or_none()
        return str(value) if value else video_id

    def add_alias(self, alias: str, canonical: str, evidence: str) -> None:
        self._video_id(alias)
        self._video_id(canonical)
        if alias == canonical or not evidence.strip():
            raise ValueError("An alias needs distinct IDs and resolution evidence")
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            if connection.execute(
                text(
                    "SELECT canonical_video_id FROM source_aliases "
                    "WHERE alias_video_id = :id"
                ),
                {"id": canonical},
            ).first():
                raise IntakeConflict("Canonical identity is itself an alias")
            prior = connection.execute(
                text(
                    "SELECT canonical_video_id FROM source_aliases "
                    "WHERE alias_video_id = :id"
                ),
                {"id": alias},
            ).scalar_one_or_none()
            if prior:
                if prior != canonical:
                    raise IntakeConflict("Alias already assigned")
                return
            states = connection.execute(
                text(
                    "SELECT video_id, state FROM source_tracks "
                    "WHERE video_id IN (:alias, :canonical)"
                ),
                {"alias": alias, "canonical": canonical},
            ).all()
            states_by_id = {str(row[0]): str(row[1]) for row in states}
            # An already completed alias needs an evidence-preserving merge, not a
            # new canonical download. Until that workflow exists, fail closed.
            if states_by_id.get(alias) in {"downloading", "downloaded"}:
                raise IntakeConflict("Active or completed aliases require review")
            connection.execute(
                text("INSERT OR IGNORE INTO source_tracks(video_id) VALUES (:id)"),
                {"id": canonical},
            )
            connection.execute(
                aliases.insert().values(
                    alias_video_id=alias,
                    canonical_video_id=canonical,
                    evidence=evidence,
                )
            )

    def next_pending(self) -> str | None:
        with self.engine.connect() as connection:
            value = connection.execute(
                text(
                    "SELECT video_id FROM source_tracks WHERE state = 'pending' "
                    "AND EXISTS (SELECT 1 FROM intake_items i "
                    "LEFT JOIN source_aliases a ON a.alias_video_id = i.video_id "
                    "LEFT JOIN intake_controls c ON c.intake_id = i.intake_id "
                    "WHERE COALESCE(a.canonical_video_id, i.video_id) "
                    "= source_tracks.video_id "
                    "AND COALESCE(c.state, 'active') = 'active') "
                    "AND video_id NOT IN (SELECT alias_video_id FROM source_aliases) "
                    "ORDER BY rowid LIMIT 1"
                )
            ).scalar_one_or_none()
            return str(value) if value else None

    def claim(self, video_id: str, *, staging_root: Path | None = None) -> Claim | None:
        self._video_id(video_id)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            canonical = self._resolve(connection, video_id)
            attempt_id = str(uuid4())
            changed = connection.execute(
                text(
                    "UPDATE source_tracks SET state = 'downloading', "
                    "lease_token = :token "
                    "WHERE video_id = :id AND state = 'pending' "
                    "AND EXISTS (SELECT 1 FROM intake_items i "
                    "LEFT JOIN source_aliases a ON a.alias_video_id = i.video_id "
                    "LEFT JOIN intake_controls c ON c.intake_id = i.intake_id "
                    "WHERE COALESCE(a.canonical_video_id, i.video_id) = :id "
                    "AND COALESCE(c.state, 'active') = 'active')"
                ),
                {"id": canonical, "token": attempt_id},
            )
            if not changed.rowcount:
                return None
            planned = (
                str(staging_root / canonical / attempt_id / f"{canonical}.m4a")
                if staging_root is not None
                else None
            )
            connection.execute(
                attempts.insert().values(
                    id=attempt_id,
                    video_id=canonical,
                    status="downloading",
                    planned_path=planned,
                )
            )
            return Claim(canonical, attempt_id)

    def planned_output(self, claim: Claim) -> Path:
        with self.engine.connect() as connection:
            value = connection.execute(
                text(
                    "SELECT planned_path FROM download_attempts "
                    "WHERE id = :id AND video_id = :video_id"
                ),
                {"id": claim.attempt_id, "video_id": claim.video_id},
            ).scalar_one()
            if value is None:
                raise ValueError("Attempt has no planned staging path")
            return Path(value)

    def inflight(self) -> list[Claim]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT video_id, lease_token FROM source_tracks "
                    "WHERE state = 'downloading' AND lease_token IS NOT NULL"
                )
            ).all()
            return [Claim(str(row[0]), str(row[1])) for row in rows]

    def fail(self, claim: Claim, reason: str) -> bool:
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            changed = connection.execute(
                text(
                    "UPDATE source_tracks SET state = 'failed', lease_token = NULL "
                    "WHERE video_id = :id AND state = 'downloading' "
                    "AND lease_token = :token"
                ),
                {"id": claim.video_id, "token": claim.attempt_id},
            )
            if changed.rowcount:
                connection.execute(
                    text(
                        "UPDATE download_attempts SET status = 'failed', "
                        "error = :reason "
                        "WHERE id = :token"
                    ),
                    {"token": claim.attempt_id, "reason": reason[:256]},
                )
            return bool(changed.rowcount)

    def retry_failed(self, video_id: str) -> bool:
        self._video_id(video_id)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            canonical = self._resolve(connection, video_id)
            changed = connection.execute(
                text(
                    "UPDATE source_tracks SET state = 'pending' "
                    "WHERE video_id = :id AND state = 'failed'"
                ),
                {"id": canonical},
            )
            return bool(changed.rowcount)

    def reconcile_missing_outputs(self, probe: Callable[[Path], VerifiedAudio]) -> int:
        """Preserve tag-only edits via decoded-audio identity; never redownload."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT video_id, audio_path, audio_sha256, pcm_sha256 "
                    "FROM source_tracks WHERE state = 'downloaded'"
                )
            ).all()
        missing = 0
        for video_id, audio_path, original_sha, pcm_sha in rows:
            path = Path(audio_path) if audio_path else None
            current_sha = hashlib.sha256()
            if path is not None and path.is_file():
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        current_sha.update(chunk)
            if (
                path is not None
                and path.is_file()
                and current_sha.hexdigest() == original_sha
            ):
                continue
            try:
                verified = probe(path) if path is not None else None
            except (ValueError, OSError):
                verified = None
            if verified is not None and pcm_sha and verified.pcm_sha256 == pcm_sha:
                with self.engine.begin() as connection:
                    connection.execute(
                        text(
                            "UPDATE source_tracks SET audio_sha256 = :new_sha "
                            "WHERE video_id = :id AND state = 'downloaded' "
                            "AND audio_sha256 = :old_sha"
                        ),
                        {
                            "id": video_id,
                            "old_sha": original_sha,
                            "new_sha": verified.sha256,
                        },
                    )
                continue
            with self.engine.begin() as connection:
                changed = connection.execute(
                    text(
                        "UPDATE source_tracks SET state = 'missing_output' "
                        "WHERE video_id = :id AND state = 'downloaded' "
                        "AND audio_sha256 = :old_sha"
                    ),
                    {"id": video_id, "old_sha": original_sha},
                )
                missing += int(bool(changed.rowcount))
        return missing

    def state(self, video_id: str) -> SourceState | None:
        canonical = self.resolve(video_id)
        with self.engine.connect() as connection:
            value = connection.execute(
                text("SELECT state FROM source_tracks WHERE video_id = :id"),
                {"id": canonical},
            ).scalar_one_or_none()
        return cast("SourceState | None", value)

    def downstream_state(self, video_id: str) -> DownstreamState:
        canonical = self.resolve(video_id)
        with self.engine.connect() as connection:
            value = connection.execute(
                text("SELECT status FROM downstream_stages WHERE video_id = :id"),
                {"id": canonical},
            ).scalar_one_or_none()
        return cast("DownstreamState", str(value) if value else "not_started")

    def tagging_candidate(self, video_id: str) -> tuple[Path, str]:
        canonical = self.resolve(video_id)
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT s.audio_path, s.pcm_sha256 FROM source_tracks s "
                    "JOIN downstream_stages d ON d.video_id = s.video_id "
                    "WHERE s.video_id = :id AND s.state = 'downloaded' "
                    "AND d.status IN ('waiting_for_tagger', 'tagged')"
                ),
                {"id": canonical},
            ).first()
            if not row or not row[0] or not row[1]:
                raise ValueError("No verified audio waiting for tags")
            return Path(row[0]), str(row[1])

    def retry_tagging(self, video_id: str) -> bool:
        """Re-open tagging without touching the verified download."""
        canonical = self.resolve(video_id)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            changed = connection.execute(
                text(
                    "UPDATE downstream_stages SET status = 'waiting_for_tagger', "
                    "tagging_evidence = NULL "
                    "WHERE video_id = :id AND status = 'tagged'"
                ),
                {"id": canonical},
            )
            return bool(changed.rowcount)

    def filing_plan(self, video_id: str) -> tuple[Path, list[tuple[str, str]]] | None:
        canonical = self.resolve(video_id)
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT planned_final_path, planned_sidecars "
                    "FROM downstream_stages WHERE video_id = :id"
                ),
                {"id": canonical},
            ).first()
        if not row or not row[0]:
            return None
        sidecars = json.loads(row[1]) if row[1] else []
        return Path(row[0]), [(str(a), str(b)) for a, b in sidecars]

    def plan_filing(
        self, video_id: str, destination: Path, sidecars: list[tuple[Path, Path]]
    ) -> None:
        """Record intent before moving, so a crash mid-move is reconcilable."""
        canonical = self.resolve(video_id)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            changed = connection.execute(
                text(
                    "UPDATE downstream_stages SET planned_final_path = :path, "
                    "planned_sidecars = :sidecars "
                    "WHERE video_id = :id AND status = 'tagged' "
                    "AND final_path IS NULL AND planned_final_path IS NULL"
                ),
                {
                    "id": canonical,
                    "path": str(destination),
                    "sidecars": json.dumps(
                        [[str(source), str(target)] for source, target in sidecars]
                    ),
                },
            )
            if not changed.rowcount:
                raise FilingRefused("Source is not ready for filing")

    def clear_filing_plan(self, video_id: str, destination: Path) -> bool:
        canonical = self.resolve(video_id)
        with self.engine.begin() as connection:
            changed = connection.execute(
                text(
                    "UPDATE downstream_stages SET planned_final_path = NULL, "
                    "planned_sidecars = NULL WHERE video_id = :id "
                    "AND planned_final_path = :path"
                ),
                {"id": canonical, "path": str(destination)},
            )
            return bool(changed.rowcount)

    def record_filed(
        self, video_id: str, audio: VerifiedAudio, final_path: Path
    ) -> str:
        canonical = self.resolve(video_id)
        if not audio.pcm_sha256:
            raise FilingRefused("Filed audio needs a decoded-audio fingerprint")
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            changed = connection.execute(
                text(
                    "UPDATE downstream_stages SET status = 'filed', "
                    "final_path = :path, planned_final_path = NULL, "
                    "planned_sidecars = NULL, error = NULL "
                    "WHERE video_id = :id AND status = 'tagged' "
                    "AND (planned_final_path IS NULL OR planned_final_path = :path) "
                    "AND EXISTS (SELECT 1 FROM source_tracks s "
                    "WHERE s.video_id = :id AND s.state = 'downloaded' "
                    "AND s.pcm_sha256 = :pcm)"
                ),
                {"id": canonical, "path": str(final_path), "pcm": audio.pcm_sha256},
            )
            if not changed.rowcount:
                raise FilingRefused("Filing target changed or audio is not verified")
            # The verified audio now lives at its final path; keep one identity.
            connection.execute(
                text(
                    "UPDATE source_tracks SET audio_path = :path, "
                    "audio_sha256 = :sha "
                    "WHERE video_id = :id AND state = 'downloaded'"
                ),
                {"id": canonical, "path": str(final_path), "sha": audio.sha256},
            )
            return "filed"

    def mark_output_missing(self, video_id: str, reason: str) -> bool:
        canonical = self.resolve(video_id)
        with self.engine.begin() as connection:
            changed = connection.execute(
                text(
                    "UPDATE downstream_stages SET status = 'missing_output', "
                    "error = :reason WHERE video_id = :id "
                    "AND status IN ('waiting_for_tagger', 'tagged', 'filed')"
                ),
                {"id": canonical, "reason": reason[:256]},
            )
            return bool(changed.rowcount)

    def pending_filings(self) -> list["FilingCandidate"]:
        """Tagged or filed sources whose recorded audio still has a known home."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT s.video_id, d.status, s.audio_path, s.pcm_sha256, "
                    "d.final_path, d.planned_final_path, d.planned_sidecars "
                    "FROM downstream_stages d JOIN source_tracks s "
                    "ON s.video_id = d.video_id "
                    "WHERE d.status IN ('tagged', 'filed') "
                    "AND s.state = 'downloaded' AND s.pcm_sha256 IS NOT NULL "
                    "ORDER BY s.video_id"
                )
            ).all()
        candidates = []
        for row in rows:
            destination = row[4] or row[5]
            if not destination:
                continue
            candidates.append(
                FilingCandidate(
                    video_id=str(row[0]),
                    status=str(row[1]),
                    source_path=Path(row[2]),
                    pcm_sha256=str(row[3]),
                    destination=Path(destination),
                    sidecars=(
                        [(str(a), str(b)) for a, b in json.loads(row[6] or "[]")]
                    ),
                )
            )
        return candidates

    def final_path(self, video_id: str) -> str | None:
        canonical = self.resolve(video_id)
        with self.engine.connect() as connection:
            value = connection.execute(
                text("SELECT final_path FROM downstream_stages WHERE video_id = :id"),
                {"id": canonical},
            ).scalar_one_or_none()
            return str(value) if value else None

    def record_tagged(self, video_id: str, audio: VerifiedAudio, evidence: str) -> str:
        canonical = self.resolve(video_id)
        if not evidence.strip() or not audio.pcm_sha256:
            raise ValueError("Tagging requires verified audio and evidence")
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            changed = connection.execute(
                text(
                    "UPDATE downstream_stages SET status = 'tagged', "
                    "tagging_evidence = :evidence WHERE video_id = :id "
                    "AND status IN ('waiting_for_tagger', 'tagged') "
                    "AND EXISTS (SELECT 1 FROM source_tracks s "
                    "WHERE s.video_id = :id AND s.state = 'downloaded' "
                    "AND s.audio_path = :path AND s.pcm_sha256 = :pcm)"
                ),
                {
                    "id": canonical,
                    "path": audio.path,
                    "pcm": audio.pcm_sha256,
                    "evidence": evidence[:512],
                },
            )
            if not changed.rowcount:
                raise IntakeConflict("Audio changed or is not waiting for tagging")
            connection.execute(
                text(
                    "UPDATE source_tracks SET audio_sha256 = :sha "
                    "WHERE video_id = :id AND state = 'downloaded'"
                ),
                {"id": canonical, "sha": audio.sha256},
            )
            return "tagged"

    def complete(self, claim: Claim, audio: VerifiedAudio) -> None:
        path = Path(audio.path)
        with self.engine.connect() as connection:
            planned = connection.execute(
                text("SELECT planned_path FROM download_attempts WHERE id = :id"),
                {"id": claim.attempt_id},
            ).scalar_one_or_none()
            if planned is not None and path != Path(planned):
                raise ValueError("Download output differs from planned staging path")
            if planned is not None and not audio.pcm_sha256:
                raise ValueError("Planned audio needs a decoded-audio fingerprint")
        digest = hashlib.sha256()
        if path.is_file():
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        if (
            not path.is_file()
            or audio.size <= 0
            or path.stat().st_size != audio.size
            or audio.duration_seconds <= 0
            or not audio.codec
            or digest.hexdigest() != audio.sha256
        ):
            raise ValueError("Audio evidence does not match a verified file")
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            changed = connection.execute(
                text(
                    "UPDATE source_tracks SET state = 'downloaded', "
                    "lease_token = NULL, "
                    "audio_path = :path, audio_sha256 = :sha, pcm_sha256 = :pcm "
                    "WHERE video_id = :id AND state = 'downloading' "
                    "AND lease_token = :token"
                ),
                {
                    "id": claim.video_id,
                    "token": claim.attempt_id,
                    "path": str(path),
                    "sha": audio.sha256,
                    "pcm": audio.pcm_sha256,
                },
            )
            if not changed.rowcount:
                raise IntakeConflict("Attempt no longer owns the source")
            connection.execute(
                text(
                    "UPDATE download_attempts SET status = 'downloaded', "
                    "audio_path = :path, audio_sha256 = :sha, pcm_sha256 = :pcm, "
                    "audio_bytes = :size, duration_seconds = :duration, "
                    "codec = :codec WHERE id = :token"
                ),
                {
                    "token": claim.attempt_id,
                    "path": str(path),
                    "sha": audio.sha256,
                    "pcm": audio.pcm_sha256,
                    "size": audio.size,
                    "duration": audio.duration_seconds,
                    "codec": audio.codec,
                },
            )
            connection.execute(
                text(
                    "INSERT OR IGNORE INTO downstream_stages(video_id, status) "
                    "VALUES (:id, 'waiting_for_tagger')"
                ),
                {"id": claim.video_id},
            )
