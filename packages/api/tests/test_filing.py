"""Filing moves verified tagged audio; crashes after a move must reconcile."""

import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from yubal_api.db.intake_ledger import IntakeLedger, metadata
from yubal_api.services.filing import (
    AlreadyFiled,
    FileConflict,
    file_verified_audio,
    reconcile_filing,
)
from yubal_api.services.intake_worker import IntakeWorker
from yubal_api.services.tag_stage import observe_manual_tags

VIDEO_ID = "dQw4w9WgXcQ"
DESTINATION = ("Main Artist", "Reviewed Title.m4a")


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
