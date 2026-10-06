"""Single-owner durable scheduler; playlist snapshots enter the same ledger."""

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import sqlalchemy as sa

from yubal_api.db.intake_controls import IntakeControls, next_run, schedules
from yubal_api.services.intake_preview import preview_playlist


class IntakeScheduler:
    def __init__(
        self,
        controls: IntakeControls,
        preview: Callable[..., dict[str, Any]] = preview_playlist,
    ) -> None:
        self.controls = controls
        self.ledger = controls.ledger
        self.preview = preview

    def prepare_next(self) -> dict[str, Any] | None:
        with self.ledger.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            row = (
                connection.execute(
                    sa.select(schedules)
                    .where(
                        schedules.c.enabled == sa.true(),
                        schedules.c.next_run <= self.ledger.clock(),
                        schedules.c.device_id.in_(
                            sa.select(sa.column("id"))
                            .select_from(sa.table("intake_devices"))
                            .where(sa.column("revoked") == 0)
                        ),
                    )
                    .order_by(schedules.c.next_run)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            result = dict(row)
            if not result["run_id"]:
                result["run_id"] = str(uuid4())
                connection.execute(
                    schedules.update()
                    .where(schedules.c.id == result["id"])
                    .values(run_id=result["run_id"])
                )
        try:
            if not result["pending_payload"]:
                self.ledger.reserve_preview(result["device_id"])
                preview = self.preview(result["playlist_id"], result["limit"])
                payload = {
                    "device_id": result["device_id"],
                    "mode": "auto_playlist",
                    "source_context": {
                        "kind": "playlist",
                        "playlist_id": result["playlist_id"],
                    },
                    "tracks": preview["tracks"],
                }
                result["pending_payload"] = json.dumps(payload)
                with self.ledger.engine.begin() as connection:
                    connection.execute(
                        schedules.update()
                        .where(schedules.c.id == result["id"])
                        .values(pending_payload=result["pending_payload"])
                    )
            return result
        except Exception as exc:
            self.finish(result, error=f"Playlist lookup failed: {type(exc).__name__}")
            return None

    def submit_prepared(self, row: dict[str, Any]) -> str:
        payload = json.loads(row["pending_payload"])
        return self.ledger.submit(
            row["run_id"],
            row["device_id"],
            "auto_playlist",
            [track["video_id"] for track in payload["tracks"]],
            details=payload,
        )

    def finish(
        self,
        row: dict[str, Any],
        *,
        intake_id: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.ledger.engine.begin() as connection:
            connection.execute(
                schedules.update()
                .where(schedules.c.id == row["id"], schedules.c.run_id == row["run_id"])
                .values(
                    run_id=None,
                    pending_payload=None,
                    next_run=next_run(row, self.ledger.clock()),
                    last_run=self.ledger.clock(),
                    last_intake_id=intake_id,
                    last_error=error,
                )
            )

    def run_once(self) -> bool:
        row = self.prepare_next()
        if row is None:
            return False
        try:
            intake_id = self.submit_prepared(row)
        except Exception as exc:
            self.finish(row, error=f"Intake submission failed: {type(exc).__name__}")
        else:
            self.finish(row, intake_id=intake_id)
        return True
