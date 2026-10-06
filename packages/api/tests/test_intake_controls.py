"""Deterministic controls; synthetic ledger evidence, never real downloads."""

import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from yubal_api.db.intake_controls import IntakeControls
from yubal_api.db.intake_ledger import (
    IntakeConflict,
    IntakeLedger,
    VerifiedAudio,
    metadata,
)


@pytest.fixture
def controls(tmp_path: Path) -> IntakeControls:
    ledger = IntakeLedger(create_engine(f"sqlite:///{tmp_path / 'controls.db'}"))
    metadata.create_all(ledger.engine)
    return IntakeControls(ledger)


def test_cancellation_keeps_shared_work_and_delete_keeps_identity(
    controls: IntakeControls,
) -> None:
    ledger = controls.ledger
    device, _ = ledger.provision_device()
    first = ledger.submit(str(uuid4()), device, "manual_song", ["dQw4w9WgXcQ"])
    second = ledger.submit(str(uuid4()), device, "manual_song", ["dQw4w9WgXcQ"])
    controls.set_intake_state(first, device, "cancelled")
    assert ledger.next_pending() == "dQw4w9WgXcQ"
    controls.set_intake_state(second, device, "deleted")
    assert ledger.next_pending() is None
    assert ledger.claim("dQw4w9WgXcQ") is None
    assert ledger.source_count() == 1
    assert ledger.history(device, limit=20, before=None)[0] == [first]
    controls.set_intake_state(first, device, "active")
    assert ledger.next_pending() == "dQw4w9WgXcQ"


def test_force_retry_is_idempotent_and_forget_preserves_files(
    controls: IntakeControls, tmp_path: Path
) -> None:
    ledger = controls.ledger
    device, _ = ledger.provision_device()
    ledger.submit(str(uuid4()), device, "manual_song", ["dQw4w9WgXcQ"])
    claim = ledger.claim("dQw4w9WgXcQ")
    assert claim is not None
    retained = tmp_path / "retained.m4a"
    retained.write_bytes(b"synthetic evidence, not playable audio")
    ledger.complete(
        claim,
        VerifiedAudio(
            str(retained),
            retained.stat().st_size,
            hashlib.sha256(retained.read_bytes()).hexdigest(),
            1,
            "aac",
        ),
    )
    request = str(uuid4())
    result = controls.track_action("dQw4w9WgXcQ", device, request, "force_redownload")
    assert result["status"] == "pending"
    claim = ledger.claim("dQw4w9WgXcQ")
    assert claim is not None
    assert (
        controls.track_action("dQw4w9WgXcQ", device, request, "force_redownload")
        == result
    )
    with pytest.raises(IntakeConflict):
        controls.track_action("dQw4w9WgXcQ", device, str(uuid4()), "forget")
    ledger.fail(claim, "synthetic failure")
    forget_id = str(uuid4())
    assert (
        controls.track_action("dQw4w9WgXcQ", device, forget_id, "forget")["status"]
        == "forgotten"
    )
    assert ledger.source_count() == 0
    assert retained.exists()
    assert (
        controls.track_action("dQw4w9WgXcQ", device, forget_id, "forget")["status"]
        == "forgotten"
    )


@pytest.mark.parametrize("second_video", ["dQw4w9WgXcQ", "jNQXAC9IVRw"])
def test_another_devices_source_cannot_be_reset(
    controls: IntakeControls, second_video: str
) -> None:
    ledger = controls.ledger
    first, _ = ledger.provision_device()
    second, _ = ledger.provision_device()
    ledger.submit(str(uuid4()), first, "manual_song", ["dQw4w9WgXcQ"])
    ledger.submit(str(uuid4()), second, "manual_song", [second_video])
    if second_video != "dQw4w9WgXcQ":
        ledger.add_alias(second_video, "dQw4w9WgXcQ", "synthetic alias evidence")
    with pytest.raises(IntakeConflict):
        controls.track_action("dQw4w9WgXcQ", first, str(uuid4()), "forget")


def test_concurrent_explicit_reset_replays_one_new_intake(
    controls: IntakeControls,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    ledger = controls.ledger
    device, _ = ledger.provision_device()
    ledger.submit(str(uuid4()), device, "manual_song", ["dQw4w9WgXcQ"])
    claim = ledger.claim("dQw4w9WgXcQ")
    assert claim is not None
    ledger.fail(claim, "synthetic failure")
    request_id = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(
                controls.track_action,
                "dQw4w9WgXcQ",
                device,
                request_id,
                "force_redownload",
            )
            for _ in range(2)
        ]
        assert calls[0].result() == calls[1].result()
    assert len(ledger.history(device, limit=20, before=None)[0]) == 2
    assert ledger.attempt_count("dQw4w9WgXcQ") == 1


def test_revoked_device_schedule_does_not_fetch_metadata(
    controls: IntakeControls,
) -> None:
    from yubal_api.services.intake_scheduler import IntakeScheduler

    device, _ = controls.ledger.provision_device()
    schedule = controls.save_schedule(
        device,
        None,
        {
            "title": "Revoked",
            "playlist_id": "PLtest",
            "cron": "0 * * * *",
            "timezone": "UTC",
            "limit": 10,
            "enabled": True,
        },
    )
    controls.run_schedule_now(schedule["id"], device)
    controls.ledger.revoke_device(device)
    assert (
        IntakeScheduler(
            controls, preview=lambda *_: pytest.fail("Revoked device fetched metadata")
        ).prepare_next()
        is None
    )


def test_schedule_recovery_reuses_saved_intent(controls: IntakeControls) -> None:
    from yubal_api.services.intake_scheduler import IntakeScheduler

    ledger = controls.ledger
    device, _ = ledger.provision_device()
    schedule = controls.save_schedule(
        device,
        None,
        {
            "title": "Test",
            "playlist_id": "PLtest",
            "cron": "0 * * * *",
            "timezone": "UTC",
            "limit": 10,
            "enabled": True,
        },
    )
    controls.run_schedule_now(schedule["id"], device)
    # Explicit fake metadata; actual SQLite scheduling/submit/replay.
    scheduler = IntakeScheduler(
        controls,
        preview=lambda _id, _limit: {
            "tracks": [{"video_id": "dQw4w9WgXcQ", "position": 0}]
        },
    )
    saved = scheduler.prepare_next()
    assert saved is not None
    intake = scheduler.submit_prepared(saved)
    # Crash window: submit committed, schedule acknowledgement did not.
    recovered = IntakeScheduler(
        controls, preview=lambda *_args: pytest.fail("Must reuse persisted snapshot")
    )
    assert recovered.run_once()
    assert ledger.history(device, limit=20, before=None)[0] == [intake]
    assert controls.list_schedules(device)[0]["last_intake_id"] == intake


def test_scheduler_process_death_after_submit_does_not_duplicate(
    controls: IntakeControls,
) -> None:
    from yubal_api.services.intake_scheduler import IntakeScheduler

    device, _ = controls.ledger.provision_device()
    schedule = controls.save_schedule(
        device,
        None,
        {
            "title": "Crash",
            "playlist_id": "PLtest",
            "cron": "0 * * * *",
            "timezone": "UTC",
            "limit": 10,
            "enabled": True,
        },
    )
    controls.run_schedule_now(schedule["id"], device)
    script = """
import os,sys
from sqlalchemy import create_engine
from yubal_api.db.intake_ledger import IntakeLedger
from yubal_api.db.intake_controls import IntakeControls
from yubal_api.services.intake_scheduler import IntakeScheduler
s=IntakeScheduler(IntakeControls(IntakeLedger(create_engine(sys.argv[1]))),
    preview=lambda *_: {'tracks':[{'video_id':'dQw4w9WgXcQ','position':0}]})
s.submit_prepared(s.prepare_next())
os._exit(23)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(controls.ledger.engine.url)], check=False
    )
    assert result.returncode == 23
    assert IntakeScheduler(
        controls, preview=lambda *_: pytest.fail("snapshot already persisted")
    ).run_once()
    assert len(controls.ledger.history(device, limit=20, before=None)[0]) == 1


def test_controls_migration_upgrade_rollback_and_backup_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from importlib.resources import files

    from alembic import command
    from alembic.config import Config
    from yubal_api.settings import get_settings

    monkeypatch.setenv("YUBAL_CONFIG", str(tmp_path / "config"))
    get_settings.cache_clear()
    config = Config(str(files("yubal_api").joinpath("alembic.ini")))
    try:
        command.upgrade(config, "7c921b71e002")
        path = get_settings().db_path
        with (
            sqlite3.connect(path) as source,
            sqlite3.connect(tmp_path / "backup.db") as backup,
        ):
            source.execute(
                "INSERT INTO source_tracks(video_id,state) "
                "VALUES ('dQw4w9WgXcQ','pending')"
            )
            source.commit()
            source.backup(backup)
        command.upgrade(config, "head")
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT count(*) FROM source_tracks").fetchone()[0] == 1
            assert (
                db.execute("SELECT count(*) FROM intake_schedules").fetchone()[0] == 0
            )
        command.downgrade(config, "7c921b71e002")
        command.upgrade(config, "head")
        with (
            sqlite3.connect(tmp_path / "backup.db") as backup,
            sqlite3.connect(tmp_path / "restored.db") as restored,
        ):
            backup.backup(restored)
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert (
                restored.execute("SELECT version_num FROM alembic_version").fetchone()[
                    0
                ]
                == "7c921b71e002"
            )
            assert (
                restored.execute("SELECT count(*) FROM source_tracks").fetchone()[0]
                == 1
            )
    finally:
        get_settings.cache_clear()
