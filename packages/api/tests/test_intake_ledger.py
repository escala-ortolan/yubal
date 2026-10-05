"""Durable intake identity; no network/download mocks are success evidence."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from yubal_api.db.intake_ledger import (
    IntakeCapacityError,
    IntakeConflict,
    IntakeLedger,
    IntakeRateLimitError,
    VerifiedAudio,
)


@pytest.fixture
def ledger(tmp_path: Path) -> IntakeLedger:
    engine = create_engine(f"sqlite:///{tmp_path / 'intake.db'}")
    # A scratch database is migrated by the migration test; use that schema here too.
    from yubal_api.db.intake_ledger import metadata

    metadata.create_all(engine)
    return IntakeLedger(engine)


def test_replay_conflict_and_ordered_duplicate_occurrences(
    ledger: IntakeLedger,
) -> None:
    request_id = str(uuid4())
    ids = ["dQw4w9WgXcQ", "aBcdEf123_0", "dQw4w9WgXcQ"]
    first = ledger.submit(request_id, "test-device", "manual_queue", ids)
    assert first == ledger.submit(request_id, "test-device", "manual_queue", ids)
    assert [item["video_id"] for item in ledger.items(first)] == ids
    assert ledger.source_count() == 2
    with pytest.raises(IntakeConflict):
        ledger.submit(request_id, "test-device", "manual_queue", ids[:2])


def test_two_submissions_get_one_atomic_claim(ledger: IntakeLedger) -> None:
    video_id = "dQw4w9WgXcQ"
    for _ in range(2):
        ledger.submit(str(uuid4()), "test-device", "manual_song", [video_id])

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(ledger.claim, [video_id, video_id]))
    assert sum(claim is not None for claim in claims) == 1
    assert ledger.attempt_count(video_id) == 1


def test_alias_requires_explicit_evidence_and_never_matches_on_title(
    ledger: IntakeLedger,
) -> None:
    canonical = "dQw4w9WgXcQ"
    alias = "aBcdEf123_0"
    ledger.submit(str(uuid4()), "test-device", "manual_song", [canonical])
    ledger.submit(str(uuid4()), "test-device", "manual_song", [alias])
    assert ledger.resolve(alias) == alias
    with pytest.raises(ValueError):
        ledger.add_alias(alias, canonical, "")
    ledger.add_alias(alias, canonical, "ytmusic-atv-resolution:verified-test")
    assert ledger.resolve(alias) == canonical
    assert ledger.claim(alias) is not None
    assert ledger.claim(canonical) is None


def test_unverified_completion_cannot_close_attempt(ledger: IntakeLedger) -> None:
    video_id = "dQw4w9WgXcQ"
    ledger.submit(str(uuid4()), "test-device", "manual_song", [video_id])
    claim = ledger.claim(video_id)
    assert claim is not None
    with pytest.raises(ValueError):
        ledger.complete(claim, VerifiedAudio("/missing.m4a", 0, "", 0, "m4a"))
    assert ledger.state(video_id) == "downloading"


def test_invalid_video_ids_are_rejected_before_storage(ledger: IntakeLedger) -> None:
    with pytest.raises(ValueError):
        ledger.submit(str(uuid4()), "test-device", "manual_song", ["http://127.0.0.1/"])
    assert ledger.source_count() == 0


def test_migration_upgrade_and_downgrade_on_scratch_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yubal_api.settings import get_settings

    monkeypatch.setenv("YUBAL_ROOT", str(tmp_path))
    monkeypatch.setenv("YUBAL_CONFIG", str(tmp_path / "config"))
    get_settings.cache_clear()
    config = Config(str(Path(__file__).parents[1] / "src/yubal_api/alembic.ini"))
    try:
        command.upgrade(config, "51c7acb10a01")
        engine = create_engine(f"sqlite:///{tmp_path / 'config/yubal/yubal.db'}")
        assert "source_tracks" in inspect(engine).get_table_names()
        assert "downstream_stages" not in inspect(engine).get_table_names()
        assert "subscriptions" in inspect(engine).get_table_names()
        ledger = IntakeLedger(engine)
        ledger.submit(str(uuid4()), "migration-device", "manual_song", ["dQw4w9WgXcQ"])
        command.upgrade(config, "head")
        assert "downstream_stages" in inspect(engine).get_table_names()
        # The pre-move filing intent must survive a real migration, not just
        # metadata.create_all in tests.
        columns = {c["name"] for c in inspect(engine).get_columns("downstream_stages")}
        assert {"planned_final_path", "planned_sidecars", "final_path"} <= columns
        assert ledger.source_count() == 1
        command.downgrade(config, "51c7acb10a01")
        assert "downstream_stages" not in inspect(engine).get_table_names()
        assert ledger.source_count() == 1
        command.downgrade(config, "03132d5514f9")
        assert "source_tracks" not in inspect(engine).get_table_names()
        assert "subscriptions" in inspect(engine).get_table_names()
    finally:
        get_settings.cache_clear()


def test_completed_source_is_not_claimable_after_restart(
    ledger: IntakeLedger, tmp_path: Path
) -> None:
    video_id = "dQw4w9WgXcQ"
    ledger.submit(str(uuid4()), "test-device", "manual_song", [video_id])
    claim = ledger.claim(video_id)
    assert claim is not None
    path = tmp_path / "staged.m4a"
    path.write_bytes(b"test fixture (not playable audio)")
    # Unit test: this exercises ledger state; the worker must probe real audio.
    ledger.complete(
        claim,
        VerifiedAudio(
            str(path),
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
            1.0,
            "m4a",
        ),
    )
    restarted = IntakeLedger(ledger.engine)
    assert restarted.claim(video_id) is None
    assert restarted.state(video_id) == "downloaded"
    assert restarted.attempt_count(video_id) == 1


def test_alias_cannot_silently_merge_existing_completed_recordings(
    ledger: IntakeLedger, tmp_path: Path
) -> None:
    alias, canonical = "aBcdEf123_0", "dQw4w9WgXcQ"
    for video_id in (alias, canonical):
        ledger.submit(str(uuid4()), "test-device", "manual_song", [video_id])
        claim = ledger.claim(video_id)
        assert claim is not None
        path = tmp_path / f"{video_id}.m4a"
        path.write_bytes(video_id.encode())
        ledger.complete(
            claim,
            VerifiedAudio(
                str(path),
                path.stat().st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
                1.0,
                "m4a",
            ),
        )
    with pytest.raises(IntakeConflict):
        ledger.add_alias(alias, canonical, "verified same metadata only")
    assert ledger.resolve(alias) == alias


def test_device_pending_queue_is_bounded_but_replay_still_works(
    ledger: IntakeLedger,
) -> None:
    replay_id = str(uuid4())
    first_batch = [f"{i:011d}" for i in range(100)]
    first = ledger.submit(replay_id, "limited-device", "manual_queue", first_batch)
    for block in range(1, 5):
        ids = [f"{i:011d}" for i in range(block * 100, (block + 1) * 100)]
        ledger.submit(str(uuid4()), "limited-device", "manual_queue", ids)
    with pytest.raises(IntakeCapacityError):
        ledger.submit(str(uuid4()), "limited-device", "manual_song", ["z" * 11])
    assert (
        ledger.submit(replay_id, "limited-device", "manual_queue", first_batch) == first
    )
    assert ledger.source_count() == 500


def test_device_rate_limit_exempts_replay_and_resets_after_window(
    tmp_path: Path,
) -> None:
    from yubal_api.db.intake_ledger import metadata

    engine = create_engine(f"sqlite:///{tmp_path / 'rate.db'}")
    metadata.create_all(engine)
    now = [1000.0]
    ledger = IntakeLedger(engine, clock=lambda: now[0])
    first_id = str(uuid4())
    first = ledger.submit(first_id, "rate-device", "manual_song", ["dQw4w9WgXcQ"])
    for _ in range(19):
        ledger.submit(str(uuid4()), "rate-device", "manual_song", ["dQw4w9WgXcQ"])
    with pytest.raises(IntakeRateLimitError):
        ledger.submit(str(uuid4()), "rate-device", "manual_song", ["dQw4w9WgXcQ"])
    assert (
        ledger.submit(first_id, "rate-device", "manual_song", ["dQw4w9WgXcQ"]) == first
    )
    now[0] += 61
    ledger.submit(str(uuid4()), "rate-device", "manual_song", ["dQw4w9WgXcQ"])
