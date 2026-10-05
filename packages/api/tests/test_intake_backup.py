"""Consistent local SQLite backup and restore onto a new scratch path."""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from yubal_api.db.intake_ledger import IntakeLedger, metadata
from yubal_api.services.intake_backup import backup_intake_db


def test_backup_restore_preserves_intent_without_overwriting_live_db(
    tmp_path: Path,
) -> None:
    original = tmp_path / "source.db"
    engine = create_engine(f"sqlite:///{original}")
    metadata.create_all(engine)
    ledger = IntakeLedger(engine)
    request_id = str(uuid4())
    intake_id = ledger.submit(request_id, "test-device", "manual_song", ["dQw4w9WgXcQ"])
    backup = tmp_path / "backup.db"
    backup_intake_db(original, backup)
    ledger.submit(str(uuid4()), "test-device", "manual_song", ["aBcdEf123_0"])
    assert ledger.source_count() == 2

    restored = tmp_path / "restored.db"
    backup_intake_db(backup, restored)
    restored_engine = create_engine(f"sqlite:///{restored}")
    restored_ledger = IntakeLedger(restored_engine)
    assert restored_ledger.source_count() == 1
    assert (
        restored_ledger.submit(
            request_id, "test-device", "manual_song", ["dQw4w9WgXcQ"]
        )
        == intake_id
    )
    assert ledger.source_count() == 2
    with pytest.raises(FileExistsError):
        backup_intake_db(original, restored)
