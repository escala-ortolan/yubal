"""Durable device-scoped job controls, explicit resets and schedules."""

import json
from datetime import datetime
from typing import Any, Literal, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from croniter import croniter

from yubal_api.db.intake_ledger import (
    IntakeConflict,
    IntakeLedger,
    IntakeRateLimitError,
    metadata,
    rate_events,
)

actions = sa.Table(
    "intake_actions",
    metadata,
    sa.Column("request_id", sa.String(36), primary_key=True),
    sa.Column("device_id", sa.String(36), nullable=False),
    sa.Column("video_id", sa.String(11), nullable=False),
    sa.Column("action", sa.String(32), nullable=False),
    sa.Column("result", sa.Text, nullable=False),
    sa.Column("previous", sa.Text, nullable=False),
)
schedules = sa.Table(
    "intake_schedules",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("device_id", sa.String(36), nullable=False, index=True),
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


def next_run(spec: dict[str, Any], now: float) -> float:
    return float(
        croniter(
            spec["cron"], datetime.fromtimestamp(now, ZoneInfo(spec["timezone"]))
        ).get_next(float)
    )


class IntakeControls:
    def __init__(self, ledger: IntakeLedger) -> None:
        self.ledger = ledger

    def intake_state(self, intake_id: str) -> Literal["active", "cancelled", "deleted"]:
        with self.ledger.engine.connect() as connection:
            return cast(
                Literal["active", "cancelled", "deleted"],
                connection.execute(
                    sa.text("SELECT state FROM intake_controls WHERE intake_id=:id"),
                    {"id": intake_id},
                ).scalar_one_or_none()
                or "active",
            )

    def set_intake_state(self, intake_id: str, device: str, state: str) -> None:
        if state not in {"active", "cancelled", "deleted"}:
            raise ValueError("Invalid intake state")
        with self.ledger.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            owner = connection.execute(
                sa.text("SELECT device_id FROM intakes WHERE id=:id"), {"id": intake_id}
            ).scalar_one_or_none()
            if owner != device:
                raise KeyError("Intake not found")
            connection.execute(
                sa.text(
                    "INSERT INTO intake_controls(intake_id,state) VALUES (:id,:state) "
                    "ON CONFLICT(intake_id) DO UPDATE SET state=:state"
                ),
                {"id": intake_id, "state": state},
            )

    def track_action(
        self, video_id: str, device: str, request_id: str, action: str
    ) -> dict[str, str]:
        if action not in {"forget", "force_redownload"}:
            raise ValueError("Invalid track action")
        ledger = self.ledger
        with ledger.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            replay = (
                connection.execute(
                    sa.select(actions).where(actions.c.request_id == request_id)
                )
                .mappings()
                .first()
            )
            if replay:
                if (replay["device_id"], replay["video_id"], replay["action"]) != (
                    device,
                    video_id,
                    action,
                ):
                    raise IntakeConflict("Request ID belongs to another action")
                return json.loads(replay["result"])
            canonical = ledger._resolve(connection, video_id)
            identities = [
                canonical,
                *connection.execute(
                    sa.text(
                        "SELECT alias_video_id FROM source_aliases "
                        "WHERE canonical_video_id=:id"
                    ),
                    {"id": canonical},
                ).scalars(),
            ]
            owners = set(
                connection.execute(
                    sa.text(
                        "SELECT DISTINCT t.device_id FROM intakes t "
                        "JOIN intake_items i ON i.intake_id=t.id "
                        "WHERE i.video_id IN :ids"
                    ).bindparams(sa.bindparam("ids", expanding=True)),
                    {"ids": identities},
                ).scalars()
            )
            if device not in owners:
                raise KeyError("Track not found")
            if owners != {device}:
                raise IntakeConflict("Track is shared with another device")
            source = (
                connection.execute(
                    sa.text("SELECT * FROM source_tracks WHERE video_id=:id"),
                    {"id": canonical},
                )
                .mappings()
                .one()
            )
            stage = (
                connection.execute(
                    sa.text("SELECT * FROM downstream_stages WHERE video_id=:id"),
                    {"id": canonical},
                )
                .mappings()
                .first()
            )
            if source["state"] == "downloading" or (
                stage and stage["planned_final_path"]
            ):
                raise IntakeConflict("Wait for active downloading or filing to finish")
            if action == "force_redownload" and source["state"] == "pending":
                raise IntakeConflict("Track is already queued")
            now = ledger.clock()
            connection.execute(
                rate_events.delete().where(rate_events.c.created_at < now - 3600)
            )
            recent = connection.scalar(
                sa.select(sa.func.count())
                .select_from(rate_events)
                .where(
                    rate_events.c.device_id == device,
                    rate_events.c.created_at > now - 60,
                )
            )
            if recent is not None and recent >= ledger.MAX_NEW_INTAKES_PER_MINUTE:
                raise IntakeRateLimitError("Device request rate exceeded")
            if action == "force_redownload":
                pending = connection.scalar(
                    sa.text(
                        "SELECT count(DISTINCT s.video_id) FROM source_tracks s "
                        "JOIN intake_items i ON i.video_id=s.video_id "
                        "JOIN intakes t ON t.id=i.intake_id "
                        "WHERE t.device_id=:device "
                        "AND s.state IN ('pending','downloading')"
                    ),
                    {"device": device},
                )
                if pending is not None and pending >= ledger.MAX_PENDING_PER_DEVICE:
                    raise IntakeConflict("Device pending queue is full")
            connection.execute(
                rate_events.insert().values(device_id=device, created_at=now)
            )
            previous = json.dumps(
                {"source": dict(source), "downstream": dict(stage) if stage else None}
            )
            if action == "force_redownload":
                connection.execute(
                    sa.text("DELETE FROM downstream_stages WHERE video_id=:id"),
                    {"id": canonical},
                )
                connection.execute(
                    sa.text(
                        "UPDATE source_tracks SET state='pending', lease_token=NULL, "
                        "audio_path=NULL, audio_sha256=NULL, pcm_sha256=NULL "
                        "WHERE video_id=:id"
                    ),
                    {"id": canonical},
                )
                # A reset must remain runnable even if its previous jobs were cancelled.
                intake_id = str(uuid4())
                details = {
                    "device_id": device,
                    "mode": "manual_song",
                    "tracks": [{"video_id": video_id, "position": 0}],
                }
                payload = json.dumps(
                    [device, "manual_song", [video_id], details],
                    sort_keys=True,
                    separators=(",", ":"),
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO intakes(id,request_id,device_id,payload) "
                        "VALUES (:id,:request,:device,:payload)"
                    ),
                    {
                        "id": intake_id,
                        "request": str(uuid4()),
                        "device": device,
                        "payload": payload,
                    },
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO intake_items(intake_id,position,video_id) "
                        "VALUES (:id,0,:video)"
                    ),
                    {"id": intake_id, "video": video_id},
                )
                result = {
                    "video_id": video_id,
                    "status": "pending",
                    "intake_id": intake_id,
                }
            else:
                for table in ("intake_items", "download_attempts", "downstream_stages"):
                    connection.execute(
                        sa.text(
                            f"DELETE FROM {table} WHERE video_id IN :ids"
                        ).bindparams(sa.bindparam("ids", expanding=True)),
                        {"ids": identities},
                    )
                connection.execute(
                    sa.text("DELETE FROM source_aliases WHERE canonical_video_id=:id"),
                    {"id": canonical},
                )
                connection.execute(
                    sa.text(
                        "DELETE FROM source_tracks WHERE video_id IN :ids"
                    ).bindparams(sa.bindparam("ids", expanding=True)),
                    {"ids": identities},
                )
                result = {"video_id": video_id, "status": "forgotten"}
            connection.execute(
                actions.insert().values(
                    request_id=request_id,
                    device_id=device,
                    video_id=video_id,
                    action=action,
                    result=json.dumps(result),
                    previous=previous,
                )
            )
            return result

    def list_schedules(self, device: str) -> list[dict[str, Any]]:
        with self.ledger.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    sa.select(schedules)
                    .where(schedules.c.device_id == device)
                    .order_by(schedules.c.next_run)
                ).mappings()
            ]

    def save_schedule(
        self, device: str, schedule_id: str | None, spec: dict[str, Any]
    ) -> dict[str, Any]:
        with self.ledger.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            if schedule_id:
                row = (
                    connection.execute(
                        sa.select(schedules).where(
                            schedules.c.id == schedule_id,
                            schedules.c.device_id == device,
                        )
                    )
                    .mappings()
                    .first()
                )
                if not row:
                    raise KeyError("Schedule not found")
                if row["run_id"]:
                    raise IntakeConflict("Schedule is currently running")
                connection.execute(
                    schedules.update()
                    .where(schedules.c.id == schedule_id)
                    .values(**spec, next_run=next_run(spec, self.ledger.clock()))
                )
            else:
                count = connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(schedules)
                    .where(schedules.c.device_id == device)
                )
                if count is not None and count >= 100:
                    raise IntakeConflict("Maximum 100 schedules per device")
                schedule_id = str(uuid4())
                connection.execute(
                    schedules.insert().values(
                        id=schedule_id,
                        device_id=device,
                        **spec,
                        next_run=next_run(spec, self.ledger.clock()),
                    )
                )
            return dict(
                connection.execute(
                    sa.select(schedules).where(schedules.c.id == schedule_id)
                )
                .mappings()
                .one()
            )

    def run_schedule_now(self, schedule_id: str, device: str) -> None:
        self._change_schedule(schedule_id, device, delete=False)

    def delete_schedule(self, schedule_id: str, device: str) -> None:
        self._change_schedule(schedule_id, device, delete=True)

    def _change_schedule(self, schedule_id: str, device: str, *, delete: bool) -> None:
        with self.ledger.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            row = (
                connection.execute(
                    sa.select(schedules).where(
                        schedules.c.id == schedule_id, schedules.c.device_id == device
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise KeyError("Schedule not found")
            if row["run_id"]:
                raise IntakeConflict("Schedule is currently running")
            if delete:
                connection.execute(
                    schedules.delete().where(schedules.c.id == schedule_id)
                )
            else:
                connection.execute(
                    schedules.update()
                    .where(schedules.c.id == schedule_id)
                    .values(enabled=True, next_run=self.ledger.clock())
                )
