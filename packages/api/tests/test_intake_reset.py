"""Offline production-reset rehearsal on local SQLite; no audio is removed."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest


def test_reset_preserves_devices_and_schedules_and_restores_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yubal_api.api.app import run_migrations
    from yubal_api.db.engine import create_db_engine
    from yubal_api.db.intake_controls import IntakeControls
    from yubal_api.db.intake_ledger import IntakeLedger
    from yubal_api.settings import get_settings

    monkeypatch.setenv("YUBAL_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("YUBAL_ROOT", str(tmp_path))
    monkeypatch.setenv("YUBAL_INTAKE_ONLY", "true")
    get_settings.cache_clear()
    try:
        run_migrations()
        path = get_settings().db_path
        ledger = IntakeLedger(create_db_engine(path))
        device, token = ledger.provision_device()
        ledger.submit(str(uuid4()), device, "manual_song", ["dQw4w9WgXcQ"])
        claim = ledger.claim("dQw4w9WgXcQ")
        assert claim is not None
        ledger.fail(claim, "Synthetic failure")
        schedule = IntakeControls(ledger).save_schedule(
            device,
            None,
            {
                "title": "Keep schedule",
                "playlist_id": "PLtest",
                "cron": "0 6 * * *",
                "timezone": "UTC",
                "limit": 10,
                "enabled": False,
            },
        )
        backup = tmp_path / "history-before-reset.db"
        command = [
            sys.executable,
            str(Path(__file__).parents[3] / "scripts/reset_intake_history.py"),
        ]
        env = dict(os.environ)
        dry = subprocess.run(
            command, env=env, capture_output=True, text=True, check=True
        )
        assert "Dry run only" in dry.stdout and token not in dry.stdout
        assert (
            ledger.source_count() == 1
            and ledger.history(device, limit=20, before=None)[0]
        )
        applied = subprocess.run(
            [*command, "--apply", "--backup", str(backup)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "After:" in applied.stdout and token not in applied.stdout
        assert backup.stat().st_mode & 0o777 == 0o600
        assert (
            ledger.source_count() == 0
            and ledger.history(device, limit=20, before=None)[0] == []
        )
        assert ledger.authenticate(token) == device
        assert IntakeControls(ledger).list_schedules(device)[0]["id"] == schedule["id"]
        assert ledger.submit(str(uuid4()), device, "manual_song", ["dQw4w9WgXcQ"])
        with (
            sqlite3.connect(backup) as before,
            sqlite3.connect(tmp_path / "restored.db") as restored,
        ):
            before.backup(restored)
            assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert (
                restored.execute("SELECT count(*) FROM source_tracks").fetchone()[0]
                == 1
            )
            assert (
                restored.execute("SELECT count(*) FROM intake_schedules").fetchone()[0]
                == 1
            )
    finally:
        get_settings.cache_clear()
