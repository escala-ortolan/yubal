"""Worker integration with real locally generated m4a and fake network transfer."""

import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from yubal_api.db.intake_ledger import IntakeLedger, metadata
from yubal_api.services.intake_worker import IntakeWorker, verify_audio

pytestmark = pytest.mark.enable_socket  # Starlette's in-process event loop socketpair.


@pytest.fixture
def sample_audio(tmp_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is required for real audio fixture")
    path = tmp_path / "sample.m4a"
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
            "-y",
            str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture
def worker_ledger(tmp_path: Path) -> IntakeLedger:
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    metadata.create_all(engine)
    ledger = IntakeLedger(engine)
    ledger.submit(str(uuid4()), "test-device", "manual_song", ["dQw4w9WgXcQ"])
    return ledger


def test_real_audio_probe_rejects_corruption(
    sample_audio: Path, tmp_path: Path
) -> None:
    assert verify_audio(sample_audio).duration_seconds > 0
    corrupt = tmp_path / "bad.m4a"
    corrupt.write_bytes(b"not audio")
    with pytest.raises(ValueError, match="audio"):
        verify_audio(corrupt)


def test_worker_uses_ledger_and_restart_never_redownloads(
    worker_ledger: IntakeLedger, sample_audio: Path, tmp_path: Path
) -> None:
    calls: list[str] = []

    def fake_transfer(video_id: str, output: Path) -> Path:
        calls.append(video_id)
        result = Path(f"{output}.m4a")
        result.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sample_audio, result)
        return result

    staging = tmp_path / "staging"
    worker = IntakeWorker(worker_ledger, staging, fake_transfer)
    assert worker.run_once() is True
    assert worker_ledger.state("dQw4w9WgXcQ") == "downloaded"
    assert worker_ledger.downstream_state("dQw4w9WgXcQ") == "waiting_for_tagger"
    assert (
        IntakeWorker(
            IntakeLedger(worker_ledger.engine), staging, fake_transfer
        ).run_once()
        is False
    )
    assert calls == ["dQw4w9WgXcQ"]


def test_metadata_name_is_flat_and_download_path_is_recorded(
    worker_ledger: IntakeLedger, sample_audio: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"

    def transfer(_video_id: str, template: Path) -> Path:
        assert template.parent == staging
        assert "%(artist,uploader|Unknown Artist)s - %(title)s" in template.name
        output = template.with_name(
            template.name.replace(
                "%(artist,uploader|Unknown Artist)s", "The Artist"
            ).replace("%(title)s", "A Title")
            + ".m4a"
        )
        shutil.copyfile(sample_audio, output)
        return output

    assert IntakeWorker(worker_ledger, staging, transfer).run_once()
    final = staging / "The Artist - A Title.m4a"
    assert final.is_file()
    assert sorted(path.name for path in staging.glob("*.m4a")) == [final.name]
    assert not any(path.is_dir() for path in staging.iterdir())
    assert worker_ledger.tagging_candidate("dQw4w9WgXcQ")[0] == final
    with worker_ledger.engine.connect() as connection:
        from sqlalchemy import text

        stored = connection.execute(
            text("SELECT planned_path,audio_path FROM download_attempts")
        ).one()
        assert stored == (str(final), str(final))


def test_filename_collision_never_overwrites_other_recording(
    worker_ledger: IntakeLedger, sample_audio: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    existing = staging / "The Artist - A Title.m4a"
    existing.write_bytes(b"another recording")

    def transfer(_video_id: str, template: Path) -> Path:
        output = template.with_name(
            template.name.replace(
                "%(artist,uploader|Unknown Artist)s", "The Artist"
            ).replace("%(title)s", "A Title")
            + ".m4a"
        )
        shutil.copyfile(sample_audio, output)
        return output

    assert IntakeWorker(worker_ledger, staging, transfer).run_once()
    assert existing.read_bytes() == b"another recording"
    assert (staging / "The Artist - A Title [dQw4w9WgXcQ].m4a").is_file()
    assert worker_ledger.state("dQw4w9WgXcQ") == "downloaded"


def test_metadata_filename_is_sanitized_and_never_escapes_staging(
    worker_ledger: IntakeLedger, sample_audio: Path, tmp_path: Path
) -> None:
    from yubal_api.services.intake_worker import sanitize_filename

    assert (
        sanitize_filename("\tArtist\\ - Title /... \x00.m4a", "dQw4w9WgXcQ")
        == "Artist - Title.m4a"
    )
    assert sanitize_filename("....m4a", "dQw4w9WgXcQ") == "dQw4w9WgXcQ.m4a"
    assert len(sanitize_filename("é" * 300 + ".m4a", "dQw4w9WgXcQ").encode()) <= 240
    staging = tmp_path / "staging"
    outside = tmp_path / "outside.m4a"
    shutil.copyfile(sample_audio, outside)
    assert IntakeWorker(worker_ledger, staging, lambda *_: outside).run_once()
    assert worker_ledger.state("dQw4w9WgXcQ") == "failed"
    assert outside.is_file()


def test_restart_recovers_flat_published_audio_without_redownloading(
    worker_ledger: IntakeLedger, sample_audio: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    claim = worker_ledger.claim("dQw4w9WgXcQ", staging_root=staging)
    assert claim is not None
    assert worker_ledger.planned_output(claim) == staging / "dQw4w9WgXcQ.m4a"
    flat = staging / "The Artist - A Title.m4a"
    worker_ledger.plan_actual_output(claim, flat)
    shutil.copyfile(sample_audio, flat)
    worker = IntakeWorker(
        worker_ledger, staging, lambda *_: pytest.fail("must not redownload")
    )
    worker.recover_startup()
    assert worker_ledger.state("dQw4w9WgXcQ") == "downloaded"
    assert worker_ledger.attempt_count("dQw4w9WgXcQ") == 1
    assert worker.run_once() is False


def test_process_death_after_metadata_publication_recovers_flat_audio(
    worker_ledger: IntakeLedger, sample_audio: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    script = """
import os, shutil, sys
from pathlib import Path
from sqlalchemy import create_engine
from yubal_api.db.intake_ledger import IntakeLedger
from yubal_api.services.intake_worker import IntakeWorker
ledger=IntakeLedger(create_engine('sqlite:///'+sys.argv[1]))
def transfer(_video_id, template):
    name=template.name.replace('%(artist,uploader|Unknown Artist)s','Recovered Artist')
    output=template.with_name(name.replace('%(title)s','Recovered Title')+'.m4a')
    shutil.copyfile(sys.argv[3], output)
    return output
ledger.complete=lambda *_: os._exit(17)
IntakeWorker(ledger, Path(sys.argv[2]), transfer).run_once()
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "worker.db"),
            str(staging),
            str(sample_audio),
        ],
        check=False,
    )
    assert child.returncode == 17
    flat = staging / "Recovered Artist - Recovered Title.m4a"
    assert flat.is_file() and flat.parent == staging
    assert worker_ledger.state("dQw4w9WgXcQ") == "downloading"
    restarted = IntakeWorker(
        worker_ledger, staging, lambda *_: pytest.fail("must recover without download")
    )
    restarted.recover_startup()
    assert worker_ledger.state("dQw4w9WgXcQ") == "downloaded"
    assert worker_ledger.attempt_count("dQw4w9WgXcQ") == 1
    assert sorted(path.name for path in staging.iterdir()) == [flat.name]
    assert restarted.run_once() is False


def test_crash_after_output_recovers_without_second_transfer(
    worker_ledger: IntakeLedger, sample_audio: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    claim = worker_ledger.claim("dQw4w9WgXcQ", staging_root=staging)
    assert claim is not None
    output = worker_ledger.planned_output(claim)
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sample_audio, output)
    transfer_calls: list[str] = []

    def unexpected_transfer(video_id: str, _output: Path) -> Path:
        transfer_calls.append(video_id)
        raise AssertionError("Recovery must not redownload")

    worker = IntakeWorker(worker_ledger, staging, unexpected_transfer)
    worker.recover_startup()
    assert worker_ledger.state("dQw4w9WgXcQ") == "downloaded"
    assert worker.run_once() is False
    assert transfer_calls == []


def test_missing_output_recovery_requires_explicit_retry(
    worker_ledger: IntakeLedger, tmp_path: Path
) -> None:
    def unexpected_transfer(_video_id: str, _output: Path) -> Path:
        raise AssertionError("Failed output must not auto-retry")

    worker = IntakeWorker(worker_ledger, tmp_path / "staging", unexpected_transfer)
    claim = worker_ledger.claim("dQw4w9WgXcQ", staging_root=tmp_path / "staging")
    assert claim is not None
    worker.recover_startup()
    assert worker_ledger.state("dQw4w9WgXcQ") == "failed"
    assert worker.run_once() is False
    assert worker_ledger.retry_failed("dQw4w9WgXcQ") is True
    assert worker_ledger.state("dQw4w9WgXcQ") == "pending"


def test_file_deleted_after_completion_is_not_an_already_saved_skip(
    worker_ledger: IntakeLedger, sample_audio: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"

    def fake_transfer(_video_id: str, output: Path) -> Path:
        result = Path(f"{output}.m4a")
        result.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sample_audio, result)
        return result

    worker = IntakeWorker(worker_ledger, staging, fake_transfer)
    assert worker.run_once()
    completed = list(staging.rglob("*.m4a"))
    assert len(completed) == 1
    completed[0].unlink()
    worker.recover_startup()
    assert worker_ledger.state("dQw4w9WgXcQ") == "missing_output"
    assert worker_ledger.downstream_state("dQw4w9WgXcQ") == "waiting_for_tagger"
    assert worker.run_once() is False


def test_corrupt_transfer_fails_and_does_not_retry_automatically(
    worker_ledger: IntakeLedger, tmp_path: Path
) -> None:
    def fake_transfer(_video_id: str, output: Path) -> Path:
        bad = Path(f"{output}.m4a")
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"partial")
        return bad

    worker = IntakeWorker(worker_ledger, tmp_path / "staging", fake_transfer)
    assert worker.run_once()
    assert worker_ledger.state("dQw4w9WgXcQ") == "failed"
    assert worker.run_once() is False
    assert worker_ledger.attempt_count("dQw4w9WgXcQ") == 1


def test_external_tag_edit_does_not_invalidate_download(
    worker_ledger: IntakeLedger, sample_audio: Path, tmp_path: Path
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    staging = tmp_path / "staging"

    def fake_transfer(_video_id: str, output: Path) -> Path:
        result = Path(f"{output}.m4a")
        result.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sample_audio, result)
        return result

    worker = IntakeWorker(worker_ledger, staging, fake_transfer)
    assert worker.run_once()
    audio_path = next(staging.rglob("*.m4a"))
    original = verify_audio(audio_path)
    updated = tmp_path / "tagged.m4a"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(audio_path),
            "-c",
            "copy",
            "-metadata",
            "title=Tagged externally",
            str(updated),
        ],
        check=True,
    )
    updated.replace(audio_path)
    assert verify_audio(audio_path).sha256 != original.sha256
    worker.recover_startup()
    assert worker_ledger.state("dQw4w9WgXcQ") == "downloaded"
    assert worker.run_once() is False
    assert worker_ledger.attempt_count("dQw4w9WgXcQ") == 1


def test_manual_external_tagger_tag_acknowledgement_checks_audio_and_tags(
    worker_ledger: IntakeLedger, sample_audio: Path, tmp_path: Path
) -> None:
    from yubal_api.services.tag_stage import observe_manual_tags

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    staging = tmp_path / "staging"

    def fake_transfer(_video_id: str, output: Path) -> Path:
        result = Path(f"{output}.m4a")
        result.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sample_audio, result)
        return result

    worker = IntakeWorker(worker_ledger, staging, fake_transfer)
    assert worker.run_once()
    with pytest.raises(ValueError, match="tag"):
        observe_manual_tags(worker_ledger, "dQw4w9WgXcQ")
    audio_path = next(staging.rglob("*.m4a"))
    tagged = tmp_path / "tagged.m4a"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(audio_path),
            "-c",
            "copy",
            "-metadata",
            "title=Reviewed Title",
            "-metadata",
            "artist=Reviewed Artist",
            str(tagged),
        ],
        check=True,
    )
    tagged.replace(audio_path)
    assert observe_manual_tags(worker_ledger, "dQw4w9WgXcQ") == "tagged"
    assert worker_ledger.downstream_state("dQw4w9WgXcQ") == "tagged"
    worker.recover_startup()
    assert worker_ledger.state("dQw4w9WgXcQ") == "downloaded"
    assert worker.run_once() is False


def test_different_playable_recording_is_not_accepted_as_tag_edit(
    worker_ledger: IntakeLedger, sample_audio: Path, tmp_path: Path
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    staging = tmp_path / "staging"

    def fake_transfer(_video_id: str, output: Path) -> Path:
        result = Path(f"{output}.m4a")
        result.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sample_audio, result)
        return result

    worker = IntakeWorker(worker_ledger, staging, fake_transfer)
    assert worker.run_once()
    audio_path = next(staging.rglob("*.m4a"))
    other = tmp_path / "other.m4a"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=1",
            "-c:a",
            "aac",
            str(other),
        ],
        check=True,
    )
    other.replace(audio_path)
    worker.recover_startup()
    assert worker_ledger.state("dQw4w9WgXcQ") == "missing_output"
    assert worker.run_once() is False


def test_app_startup_recovers_under_exclusive_worker_lock(
    tmp_path: Path, sample_audio: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    from yubal_api.api.app import create_app, run_migrations
    from yubal_api.db.engine import create_db_engine
    from yubal_api.settings import get_settings

    monkeypatch.setenv("YUBAL_ROOT", str(tmp_path))
    monkeypatch.setenv("YUBAL_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("YUBAL_DATA", str(tmp_path / "not-served"))
    monkeypatch.setenv("YUBAL_INTAKE_ONLY", "true")
    monkeypatch.setenv("YUBAL_INTAKE_WORKER_ENABLED", "true")
    staging = tmp_path / "staging"
    monkeypatch.setenv("YUBAL_INTAKE_STAGING", str(staging))
    get_settings.cache_clear()
    try:
        run_migrations()
        engine = create_db_engine(get_settings().db_path)
        ledger = IntakeLedger(engine)
        device_id, token = ledger.provision_device()
        request_id = str(uuid4())
        intake_id = ledger.submit(request_id, device_id, "manual_song", ["dQw4w9WgXcQ"])
        claim = ledger.claim("dQw4w9WgXcQ", staging_root=staging)
        assert claim is not None
        output = ledger.planned_output(claim)
        output.parent.mkdir(parents=True)
        shutil.copyfile(sample_audio, output)
        engine.dispose()
        with TestClient(create_app()) as client:
            response = client.get(
                f"/v1/intakes/{intake_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.json()["items"][0]["status"] == "downloaded"
            with pytest.raises(BlockingIOError):
                with TestClient(create_app()):
                    pass
    finally:
        get_settings.cache_clear()


def test_process_death_after_audio_before_ledger_commit(
    worker_ledger: IntakeLedger, sample_audio: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    # This is a separate interpreter ending abruptly: no context-manager cleanup.
    script = """
import os, shutil, sys
from pathlib import Path
from sqlalchemy import create_engine
from yubal_api.db.intake_ledger import IntakeLedger
ledger = IntakeLedger(create_engine('sqlite:///' + sys.argv[1]))
claim = ledger.claim('dQw4w9WgXcQ', staging_root=Path(sys.argv[2]))
assert claim is not None
output = ledger.planned_output(claim)
output.parent.mkdir(parents=True)
shutil.copyfile(sys.argv[3], output)
os._exit(17)
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "worker.db"),
            str(staging),
            str(sample_audio),
        ],
        check=False,
    )
    assert child.returncode == 17
    assert worker_ledger.state("dQw4w9WgXcQ") == "downloading"

    def unexpected_transfer(_video_id: str, _output: Path) -> Path:
        raise AssertionError("Recovered output must not be transferred again")

    worker = IntakeWorker(
        IntakeLedger(worker_ledger.engine), staging, unexpected_transfer
    )
    worker.recover_startup()
    assert worker_ledger.state("dQw4w9WgXcQ") == "downloaded"
    assert worker.run_once() is False
    assert worker_ledger.attempt_count("dQw4w9WgXcQ") == 1


def test_proven_alias_after_download_reuses_verified_audio(
    worker_ledger: IntakeLedger, sample_audio: Path, tmp_path: Path
) -> None:
    canonical = "dQw4w9WgXcQ"
    alias = "aBcdEf123_0"
    alias_intake = worker_ledger.submit(
        str(uuid4()), "test-device", "manual_song", [alias]
    )
    calls: list[str] = []

    def fake_transfer(video_id: str, output: Path) -> Path:
        calls.append(video_id)
        result = Path(f"{output}.m4a")
        result.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sample_audio, result)
        return result

    worker = IntakeWorker(worker_ledger, tmp_path / "staging", fake_transfer)
    # The canonical source was submitted first; its only transfer is validated.
    assert worker.run_once()
    worker_ledger.add_alias(alias, canonical, "ytmusic-atv-resolution:verified")
    assert worker_ledger.state(alias) == "downloaded"
    assert worker_ledger.items(alias_intake)[0]["status"] == "downloaded"
    assert worker.run_once() is False
    assert calls == [canonical]
    assert worker_ledger.attempt_count(alias) == 1
