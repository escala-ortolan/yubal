"""Generated local audio, owner registration and real filing; no network transfers."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from yubal_api.db.intake_ledger import IntakeLedger, metadata
from yubal_api.services.intake_worker import IntakeWorker
from yubal_api.services.manual_intake import register_manual_paths
from yubal_api.services.tag_stage import observe_manual_tags
from yubal_api.services.tagger_handoff import file_reviewed, preview_reviewed


@pytest.fixture
def local_source(tmp_path: Path) -> tuple[IntakeLedger, Path, Path]:
    root = tmp_path / "_intake"
    root.mkdir()
    source = root / "Untrusted filename.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=0.1",
            "-c:a",
            "libmp3lame",
            "-metadata",
            "artist=Manual Artist",
            "-metadata",
            "title=Tagged Title",
            str(source),
        ],
        check=True,
    )
    old = 1_700_000_000
    os.utime(source, (old, old))
    engine = create_engine("sqlite:///" + str(tmp_path / "ledger.db"))
    metadata.create_all(engine)
    return IntakeLedger(engine), root, source


def test_manual_arrival_gets_local_identity_not_a_download_attempt(
    local_source: tuple[IntakeLedger, Path, Path], tmp_path: Path
) -> None:
    ledger, root, source = local_source
    before = source.read_bytes()
    result = register_manual_paths(ledger, root, [source])
    item = result[0]
    sid = item["source_id"]
    assert sid.startswith("manual-") and len(sid) == 39
    assert item["source_kind"] == "manual" and "video_id" not in item
    assert ledger.attempt_count(sid) == 0
    assert ledger.downstream_state(sid) == "waiting_for_tagger"
    assert register_manual_paths(ledger, root, [source])[0]["source_id"] == sid
    assert source.read_bytes() == before
    assert not IntakeWorker(
        ledger, root, lambda *_: pytest.fail("manual source transferred")
    ).run_once()
    with pytest.raises(ValueError, match="YouTube"):
        ledger.submit(str(uuid4()), "fixture", "manual_song", [sid])
    assert observe_manual_tags(ledger, sid) == "tagged"
    proposal = preview_reviewed(ledger, sid, tmp_path / "Music")
    result = file_reviewed(ledger, sid, tmp_path / "Music", proposal["token"])
    assert result["status"] == "filed"
    assert Path(result["path"]).read_bytes() == before and not source.exists()
    assert ledger.final_path(sid) == result["path"]
    assert (
        file_reviewed(ledger, sid, tmp_path / "Music", proposal["token"])["path"]
        == result["path"]
    )


def test_same_filename_after_filing_is_a_new_arrival(
    local_source: tuple[IntakeLedger, Path, Path], tmp_path: Path
) -> None:
    ledger, root, source = local_source
    sid = register_manual_paths(ledger, root, [source])[0]["source_id"]
    assert observe_manual_tags(ledger, sid) == "tagged"
    proposal = preview_reviewed(ledger, sid, tmp_path / "Music")
    result = file_reviewed(ledger, sid, tmp_path / "Music", proposal["token"])
    shutil.copyfile(result["path"], source)
    os.utime(source, (1_700_000_000, 1_700_000_000))
    new = register_manual_paths(ledger, root, [source])[0]["source_id"]
    assert sid != new and ledger.downstream_state(sid) == "filed"


def test_manual_identity_is_not_guessed_from_content_or_reused_for_other_paths(
    local_source: tuple[IntakeLedger, Path, Path],
) -> None:
    ledger, root, source = local_source
    other = root / "Duplicate audio.mp3"
    shutil.copyfile(source, other)
    os.utime(other, (1_700_000_000, 1_700_000_000))
    rows = register_manual_paths(ledger, root, [source, other])
    assert len({r["source_id"] for r in rows}) == 2
    assert source.exists() and other.exists()


def test_mixed_youtube_and_manual_arrivals_keep_their_provenance(
    local_source: tuple[IntakeLedger, Path, Path],
) -> None:
    ledger, root, source = local_source
    video = "dQw4w9WgXcQ"
    ledger.submit(str(uuid4()), "fixture", "manual_song", [video])

    def transfer(_video: str, output: Path) -> Path:
        path = Path(str(output) + ".mp3")
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
        return path

    assert IntakeWorker(ledger, root, transfer).run_once()
    work = ledger.tagger_work(root)
    youtube_path = Path(next(r["path"] for r in work if r["source_id"] == video))
    os.utime(youtube_path, (1_700_000_000, 1_700_000_000))
    rows = register_manual_paths(ledger, root, [source, youtube_path])
    assert {r["source_kind"] for r in rows} == {"manual", "youtube"}
    assert next(r for r in rows if r["source_kind"] == "youtube")["video_id"] == video
    manual = next(r for r in rows if r["source_kind"] == "manual")
    assert (
        ledger.attempt_count(video) == 1
        and ledger.attempt_count(manual["source_id"]) == 0
    )


def test_concurrent_owner_registration_is_idempotent(
    local_source: tuple[IntakeLedger, Path, Path],
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    ledger, root, source = local_source
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(register_manual_paths, ledger, root, [source]) for _ in range(2)
        ]
        assert len({f.result()[0]["source_id"] for f in futures}) == 1


def test_real_migration_keeps_existing_sources_and_refuses_lossy_downgrade(
    local_source: tuple[IntakeLedger, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alembic import command
    from alembic.config import Config
    from yubal_api.settings import get_settings

    _, root, source = local_source
    monkeypatch.setenv("YUBAL_CONFIG", str(tmp_path / "migrated"))
    get_settings.cache_clear()
    config = Config(str(Path(__file__).parents[1] / "src/yubal_api/alembic.ini"))
    try:
        command.upgrade(config, "d42b6c1a0004")
        engine = create_engine("sqlite:///" + str(get_settings().db_path))
        old = IntakeLedger(engine)
        old.submit(str(uuid4()), "fixture", "manual_song", ["dQw4w9WgXcQ"])
        command.upgrade(config, "head")
        row = register_manual_paths(old, root, [source])[0]
        assert old.state("dQw4w9WgXcQ") == "pending"
        assert old.state(row["source_id"]) == "downloaded"
        with pytest.raises(RuntimeError, match="Manual records"):
            command.downgrade(config, "d42b6c1a0004")
        assert old.tagging_candidate(row["source_id"])[0] == source
    finally:
        get_settings.cache_clear()


def test_registration_rejects_escape_symlinks_young_or_changed_files(
    local_source: tuple[IntakeLedger, Path, Path], tmp_path: Path
) -> None:
    ledger, root, source = local_source
    with pytest.raises(ValueError):
        register_manual_paths(ledger, root, [tmp_path / "outside.mp3"])
    symlink = root / "link.mp3"
    symlink.symlink_to(source)
    with pytest.raises(ValueError):
        register_manual_paths(ledger, root, [symlink])
    os.utime(source, None)
    with pytest.raises(ValueError, match=r"stable|settle"):
        register_manual_paths(ledger, root, [source])


def test_replaced_registered_recording_is_never_silently_adopted(
    local_source: tuple[IntakeLedger, Path, Path],
) -> None:
    ledger, root, source = local_source
    sid = register_manual_paths(ledger, root, [source])[0]["source_id"]
    source.write_bytes(b"not playable audio")
    os.utime(source, (1_700_000_000, 1_700_000_000))
    with pytest.raises(ValueError):
        register_manual_paths(ledger, root, [source])
    assert ledger.attempt_count(sid) == 0


def test_registration_does_not_reset_tagged_source(
    local_source: tuple[IntakeLedger, Path, Path],
) -> None:
    ledger, root, source = local_source
    sid = register_manual_paths(ledger, root, [source])[0]["source_id"]
    assert observe_manual_tags(ledger, sid) == "tagged"
    with pytest.raises(ValueError, match=r"state|waiting|review"):
        register_manual_paths(ledger, root, [source])
    assert ledger.downstream_state(sid) == "tagged"


def test_invalid_recording_does_not_leave_a_manual_ledger_row(
    local_source: tuple[IntakeLedger, Path, Path],
) -> None:
    ledger, root, source = local_source
    source.write_bytes(b"broken")
    with pytest.raises(ValueError):
        register_manual_paths(ledger, root, [source], settle_seconds=0)
    with ledger.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM manual_sources")).scalar_one() == 0


def test_process_death_after_registration_reuses_the_same_local_identity(
    local_source: tuple[IntakeLedger, Path, Path], tmp_path: Path
) -> None:
    ledger, root, source = local_source
    script = """
import os,sys
from pathlib import Path
from sqlalchemy import create_engine
from yubal_api.db.intake_ledger import IntakeLedger
from yubal_api.services.manual_intake import register_manual_paths
l=IntakeLedger(create_engine('sqlite:///'+sys.argv[1]))
register_manual_paths(l,Path(sys.argv[2]),[Path(sys.argv[3])])
os._exit(29)
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "ledger.db"),
            str(root),
            str(source),
        ],
        check=False,
    )
    assert child.returncode == 29
    before = ledger.tagger_work(root)[0]["source_id"]
    assert register_manual_paths(ledger, root, [source])[0]["source_id"] == before
    assert ledger.attempt_count(before) == 0


def test_manual_replacement_recovers_after_real_process_death(
    local_source: tuple[IntakeLedger, Path, Path], tmp_path: Path
) -> None:
    ledger, root, source = local_source
    sid = register_manual_paths(ledger, root, [source])[0]["source_id"]
    assert observe_manual_tags(ledger, sid) == "tagged"
    music = tmp_path / "Music"
    destination = Path(preview_reviewed(ledger, sid, music)["destination"])
    destination.parent.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=0.1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "32k",
            str(destination),
        ],
        check=True,
    )
    original = destination.read_bytes()
    proposal = preview_reviewed(ledger, sid, music)
    assert proposal["quality_decision"] == "incoming"
    script = """
import os,sys
from pathlib import Path
from sqlalchemy import create_engine
from yubal_api.db.intake_ledger import IntakeLedger
from yubal_api.services.tagger_handoff import file_reviewed
l=IntakeLedger(create_engine('sqlite:///'+sys.argv[1]))
file_reviewed(l,sys.argv[2],Path(sys.argv[3]),sys.argv[4],
    allow_replace=True,after_archive=lambda p:os._exit(29))
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "ledger.db"),
            sid,
            str(music),
            proposal["token"],
        ],
        check=False,
    )
    assert child.returncode == 29 and source.exists() and not destination.exists()
    result = file_reviewed(ledger, sid, music, proposal["token"], allow_replace=True)
    assert (
        result["status"] == "filed" and Path(result["backup"]).read_bytes() == original
    )
    assert ledger.attempt_count(sid) == 0
