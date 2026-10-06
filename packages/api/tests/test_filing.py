"""Filing moves verified tagged audio; crashes after a move must reconcile."""

import errno
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from yubal_api.db.intake_ledger import IntakeLedger, metadata
from yubal_api.services.filing import (
    AlreadyFiled,
    FileConflict,
    _move_no_clobber,
    file_verified_audio,
    reconcile_filing,
)
from yubal_api.services.intake_worker import IntakeWorker
from yubal_api.services.tag_stage import observe_manual_tags

VIDEO_ID = "dQw4w9WgXcQ"
DESTINATION = ("Main Artist", "Reviewed Title.m4a")


def test_atomic_publication_never_overwrites_a_concurrent_winner(
    tmp_path: Path,
) -> None:
    from threading import Barrier

    destination = tmp_path / "winner.m4a"
    sources = [tmp_path / "a.m4a", tmp_path / "b.m4a"]
    for index, source in enumerate(sources):
        source.write_bytes(bytes([index]) * 1024)
    barrier = Barrier(2)

    def publish(source: Path) -> bool:
        barrier.wait()
        try:
            _move_no_clobber(source, destination)
            return True
        except FileConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, sources))
    assert sorted(results) == [False, True]
    winner = results.index(True)
    assert destination.read_bytes() == bytes([winner]) * 1024
    assert sources[1 - winner].is_file()


def test_publication_refuses_dangling_destination_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"keep source")
    target = tmp_path / "target"
    target.symlink_to(tmp_path / "absent")
    with pytest.raises(FileConflict):
        _move_no_clobber(source, target)
    assert target.is_symlink()
    assert source.read_bytes() == b"keep source"


def test_cross_filesystem_publication_is_complete_before_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate EXDEV; inspect the complete temporary file at publication."""
    source = tmp_path / "source"
    source.write_bytes(b"complete content" * 1000)
    destination = tmp_path / "library" / "target"
    real_link = os.link
    publications = []

    def cross_device_link(src: object, dst: object, **kwargs: object) -> None:
        if src == source:
            raise OSError(errno.EXDEV, "Simulated cross-filesystem link")
        assert Path(str(src)).read_bytes() == source.read_bytes()
        publications.append(src)
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", cross_device_link)
    _move_no_clobber(source, destination)
    assert destination.read_bytes() == b"complete content" * 1000
    assert not source.exists()
    assert list(destination.parent.iterdir()) == [destination]
    assert len(publications) == 1


def test_cross_filesystem_copy_loses_race_without_overwriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"source recording")
    destination = tmp_path / "library" / "target"
    real_link = os.link

    def competing_link(src: object, dst: object, **kwargs: object) -> None:
        if src == source:
            raise OSError(errno.EXDEV, "Simulated cross-filesystem link")
        destination.write_bytes(b"concurrent winner")
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", competing_link)
    with pytest.raises(FileConflict):
        _move_no_clobber(source, destination)
    assert source.read_bytes() == b"source recording"
    assert destination.read_bytes() == b"concurrent winner"
    assert list(destination.parent.iterdir()) == [destination]


@pytest.fixture
def tagged_source(tmp_path: Path) -> tuple[IntakeLedger, Path]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is needed for real audio/metadata fixture")
    ledger = IntakeLedger(create_engine(f"sqlite:///{tmp_path / 'ledger.db'}"))
    metadata.create_all(ledger.engine)
    ledger.submit(str(uuid4()), "test-device", "manual_song", [VIDEO_ID])
    sample = tmp_path / "sample.m4a"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=1",
            "-c:a",
            "aac",
            "-metadata",
            "album_artist=Main Artist",
            "-metadata",
            "title=Reviewed Title",
            "-metadata",
            "album=Reviewed Album",
            "-metadata",
            "genre=Rock",
            str(sample),
        ],
        check=True,
    )

    def fake_transfer(_video_id: str, output: Path) -> Path:
        result = Path(f"{output}.m4a")
        result.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sample, result)
        return result

    staging = tmp_path / "staging"
    assert IntakeWorker(ledger, staging, fake_transfer).run_once()
    audio_path = next(staging.rglob("*.m4a"))
    audio_path.with_suffix(".lrc").write_text("[00:00.00]Reviewed lyrics")
    audio_path.with_suffix(".jpg").write_bytes(b"test artwork sidecar")
    assert observe_manual_tags(ledger, VIDEO_ID) == "tagged"
    return ledger, audio_path


def test_filing_moves_audio_sidecars_and_records_final_path(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, source = tagged_source
    music_root = tmp_path / "Music"
    filed = file_verified_audio(ledger, VIDEO_ID, music_root)
    assert filed.audio_path == music_root.joinpath(*DESTINATION)
    assert filed.audio_path.is_file()
    assert not source.exists()
    assert sorted(p.name for p in filed.audio_path.parent.iterdir()) == [
        "Reviewed Title.jpg",
        "Reviewed Title.lrc",
        "Reviewed Title.m4a",
    ]
    assert filed.sidecars == (
        filed.audio_path.with_suffix(".lrc"),
        filed.audio_path.with_suffix(".jpg"),
    )
    assert ledger.downstream_state(VIDEO_ID) == "filed"
    assert ledger.final_path(VIDEO_ID) == str(filed.audio_path)
    assert ledger.state(VIDEO_ID) == "downloaded"
    assert ledger.attempt_count(VIDEO_ID) == 1


def test_filing_refuses_a_case_conflicting_target_and_writes_nothing(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, source = tagged_source
    music_root = tmp_path / "Music"
    existing = music_root / "main artist" / "reviewed title.m4a"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"different recording")
    with pytest.raises(FileConflict):
        file_verified_audio(ledger, VIDEO_ID, music_root)
    assert existing.read_bytes() == b"different recording"
    assert source.is_file()
    assert ledger.downstream_state(VIDEO_ID) == "tagged"
    assert ledger.filing_plan(VIDEO_ID) is None


def test_process_death_after_move_before_ledger_write_recovers_filed(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    _, source = tagged_source
    music_root = tmp_path / "Music"
    # A separate interpreter records the plan, moves the audio, then dies hard
    # before the ledger records the final path.
    script = """
import os, sys
from pathlib import Path
from sqlalchemy import create_engine
from yubal_api.db.intake_ledger import IntakeLedger
from yubal_api.services.filing import file_verified_audio
ledger = IntakeLedger(create_engine('sqlite:///' + sys.argv[1]))
file_verified_audio(
    ledger,
    'dQw4w9WgXcQ',
    Path(sys.argv[2]),
    after_audio_move=lambda _p: os._exit(17),
)
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "ledger.db"),
            str(music_root),
        ],
        check=False,
    )
    assert child.returncode == 17
    destination = music_root.joinpath(*DESTINATION)
    assert destination.is_file()
    assert not source.exists()
    # The audio move landed; only the sidecar move and ledger write did not.
    assert not destination.with_suffix(".lrc").exists()
    assert source.with_suffix(".lrc").is_file()

    recovered = IntakeLedger(create_engine(f"sqlite:///{tmp_path / 'ledger.db'}"))
    assert recovered.downstream_state(VIDEO_ID) == "tagged"
    assert reconcile_filing(recovered) == 1
    assert recovered.downstream_state(VIDEO_ID) == "filed"
    assert recovered.final_path(VIDEO_ID) == str(destination)
    assert recovered.state(VIDEO_ID) == "downloaded"
    assert recovered.attempt_count(VIDEO_ID) == 1
    assert destination.with_suffix(".lrc").is_file()
    assert not source.with_suffix(".lrc").exists()

    with pytest.raises(AlreadyFiled):
        file_verified_audio(recovered, VIDEO_ID, music_root)
    assert recovered.attempt_count(VIDEO_ID) == 1


def test_real_cross_filesystem_death_after_publication_recovers(
    tagged_source: tuple[IntakeLedger, Path],
    tmp_path: Path,
) -> None:
    """Actual EXDEV + process death, with locally generated audio, no network."""
    if (
        not Path("/dev/shm").is_dir()
        or Path("/dev/shm").stat().st_dev == tmp_path.stat().st_dev
    ):
        pytest.skip("A separate writable filesystem is required")
    ledger, source = tagged_source
    with tempfile.TemporaryDirectory(
        prefix="yubal-filing-", dir="/dev/shm"
    ) as other_fs:
        script = """
import os, sys
from pathlib import Path
from sqlalchemy import create_engine
from yubal_api.db.intake_ledger import IntakeLedger
from yubal_api.services.filing import file_verified_audio
original_unlink = Path.unlink
def die_before_source_unlink(self, *args, **kwargs):
    if str(self) == sys.argv[3]:
        os._exit(19)
    return original_unlink(self, *args, **kwargs)
Path.unlink = die_before_source_unlink
ledger = IntakeLedger(create_engine('sqlite:///' + sys.argv[1]))
file_verified_audio(ledger, 'dQw4w9WgXcQ', Path(sys.argv[2]))
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(tmp_path / "ledger.db"),
                other_fs,
                str(source),
            ],
            check=False,
        )
        assert result.returncode == 19
        destination = Path(other_fs).joinpath(*DESTINATION)
        assert source.is_file() and destination.is_file()
        assert source.read_bytes() == destination.read_bytes()
        assert reconcile_filing(ledger) == 1
        assert not source.exists()
        assert ledger.final_path(VIDEO_ID) == str(destination)
        assert ledger.downstream_state(VIDEO_ID) == "filed"
        assert ledger.attempt_count(VIDEO_ID) == 1
        assert destination.with_suffix(".lrc").is_file()
        assert destination.with_suffix(".jpg").is_file()
        assert reconcile_filing(ledger) == 0


def test_concurrent_filing_retry_waits_for_active_plan(
    tagged_source: tuple[IntakeLedger, Path],
    tmp_path: Path,
) -> None:
    from threading import Event

    ledger, _ = tagged_source
    moved, release, retry_started = Event(), Event(), Event()

    def pause_after_move(_path: Path) -> None:
        moved.set()
        assert release.wait(timeout=5)

    def retry() -> None:
        retry_started.set()
        with pytest.raises(AlreadyFiled):
            file_verified_audio(ledger, VIDEO_ID, tmp_path / "Other Music")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            file_verified_audio,
            ledger,
            VIDEO_ID,
            tmp_path / "Music",
            after_audio_move=pause_after_move,
        )
        try:
            assert moved.wait(timeout=5)
            second = pool.submit(retry)
            assert retry_started.wait(timeout=5)
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
        finally:
            release.set()
        filed = first.result(timeout=5)
        second.result(timeout=5)
    assert ledger.final_path(VIDEO_ID) == str(filed.audio_path)
    assert ledger.attempt_count(VIDEO_ID) == 1
    assert not (tmp_path / "Other Music").exists()


def test_interrupted_filing_that_never_moved_stays_filable(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, source = tagged_source
    music_root = tmp_path / "Music"
    ledger.plan_filing(
        VIDEO_ID,
        music_root.joinpath(*DESTINATION),
        [
            (
                source.with_suffix(".lrc"),
                music_root.joinpath(*DESTINATION[:1], "Reviewed Title.lrc"),
            )
        ],
    )
    assert ledger.filing_plan(VIDEO_ID) is not None
    assert reconcile_filing(ledger) == 0
    assert ledger.filing_plan(VIDEO_ID) is None
    assert ledger.downstream_state(VIDEO_ID) == "tagged"
    filed = file_verified_audio(ledger, VIDEO_ID, music_root)
    assert filed.audio_path.is_file()


def test_filed_output_deleted_is_missing_output_not_a_redownload(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, _ = tagged_source
    filed = file_verified_audio(ledger, VIDEO_ID, tmp_path / "Music")
    filed.audio_path.unlink()
    assert reconcile_filing(ledger) == 1
    assert ledger.downstream_state(VIDEO_ID) == "missing_output"
    assert ledger.state(VIDEO_ID) == "downloaded"
    assert ledger.attempt_count(VIDEO_ID) == 1


def test_filed_output_replaced_by_another_recording_is_missing_output(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, _ = tagged_source
    filed = file_verified_audio(ledger, VIDEO_ID, tmp_path / "Music")
    other = filed.audio_path.with_name("other.m4a")
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=2",
            "-c:a",
            "aac",
            str(other),
        ],
        check=True,
    )
    other.replace(filed.audio_path)
    assert reconcile_filing(ledger) == 1
    assert ledger.downstream_state(VIDEO_ID) == "missing_output"
    assert ledger.state(VIDEO_ID) == "downloaded"


def test_tagging_retry_never_touches_the_download(
    tagged_source: tuple[IntakeLedger, Path],
) -> None:
    ledger, _ = tagged_source
    assert ledger.retry_tagging(VIDEO_ID) is True
    assert ledger.downstream_state(VIDEO_ID) == "waiting_for_tagger"
    assert ledger.state(VIDEO_ID) == "downloaded"
    assert ledger.attempt_count(VIDEO_ID) == 1
    assert observe_manual_tags(ledger, VIDEO_ID) == "tagged"


@pytest.mark.enable_socket
def test_app_startup_recovers_filing_before_missing_output_scan(
    tagged_source: tuple[IntakeLedger, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real generated audio and DB, simulated interruption after the move."""
    import importlib

    from fastapi.testclient import TestClient
    from yubal_api.settings import get_settings

    module = importlib.import_module("yubal_api.api.app")
    ledger, source = tagged_source
    metadata.create_all(ledger.engine)
    destination = tmp_path / "Music" / "recovered.m4a"
    destination.parent.mkdir()
    ledger.plan_filing(VIDEO_ID, destination, [])
    source.replace(destination)
    monkeypatch.setenv("YUBAL_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("YUBAL_DATA", str(tmp_path / "library"))
    monkeypatch.setenv("YUBAL_INTAKE_ONLY", "true")
    monkeypatch.setenv("YUBAL_INTAKE_WORKER_ENABLED", "true")
    monkeypatch.setenv("YUBAL_INTAKE_STAGING", str(tmp_path / "staging"))
    get_settings.cache_clear()
    get_settings().db_path.parent.mkdir(parents=True)
    # The fixture owns an already-created isolated DB; exercise real lifespan
    # recovery/worker logic while substituting only its DB construction/migrations.
    monkeypatch.setattr(module, "run_migrations", lambda: None)
    monkeypatch.setattr(module, "create_db_engine", lambda _path: ledger.engine)
    try:
        with TestClient(module.create_app()) as client:
            recovered = client.app.state.intake_ledger
            assert recovered.state(VIDEO_ID) == "downloaded"
            assert recovered.downstream_state(VIDEO_ID) == "filed"
            assert recovered.final_path(VIDEO_ID) == str(destination)
            assert recovered.attempt_count(VIDEO_ID) == 1
    finally:
        get_settings.cache_clear()
