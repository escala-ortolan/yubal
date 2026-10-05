"""Read-only filing preview after separate, verified manual tagging."""

import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from yubal_api.db.intake_ledger import IntakeLedger, metadata
from yubal_api.services.filing_preview import preview_filing
from yubal_api.services.intake_worker import IntakeWorker
from yubal_api.services.tag_stage import observe_manual_tags


@pytest.fixture
def tagged_source(tmp_path: Path) -> tuple[IntakeLedger, Path]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is needed for real audio/metadata fixture")
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    metadata.create_all(engine)
    ledger = IntakeLedger(engine)
    ledger.submit(str(uuid4()), "test-device", "manual_song", ["dQw4w9WgXcQ"])
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
            "album_artist=Main Artist feat. Guest",
            "-metadata",
            "artist=Main Artist & Friends",
            "-metadata",
            "title=Song (Live Version)",
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
    audio_path.with_suffix(".lrc").write_text("Test lyrics")
    audio_path.with_suffix(".jpg").write_bytes(b"test artwork sidecar")
    assert observe_manual_tags(ledger, "dQw4w9WgXcQ") == "tagged"
    return ledger, audio_path


def test_preview_preserves_recording_version_sidecars_and_curated_tags(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, source = tagged_source
    root = tmp_path / "Music"
    proposal = preview_filing(ledger, "dQw4w9WgXcQ", root)
    assert proposal.source == source
    assert proposal.destination == root / "Main Artist" / "Song (Live Version).m4a"
    assert proposal.sidecars == (
        (source.with_suffix(".lrc"), proposal.destination.with_suffix(".lrc")),
        (source.with_suffix(".jpg"), proposal.destination.with_suffix(".jpg")),
    )
    assert proposal.conflicts == ()
    assert not root.exists()  # Preview never writes to a library.


def test_preview_refuses_case_insensitive_existing_recording_collision(
    tagged_source: tuple[IntakeLedger, Path], tmp_path: Path
) -> None:
    ledger, _ = tagged_source
    root = tmp_path / "Music"
    existing = root / "main artist"
    existing.mkdir(parents=True)
    (existing / "song (live version).m4a").write_bytes(b"different recording")
    proposal = preview_filing(ledger, "dQw4w9WgXcQ", root)
    assert proposal.conflicts
    assert (existing / "song (live version).m4a").read_bytes() == b"different recording"
