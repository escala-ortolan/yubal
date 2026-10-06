"""Move already-tagged verified audio into a flat library, recoverably.

Filing only ever writes into the music root the caller names, never touches an
existing file there, and records its intent in the ledger before the first move
so an interrupted filing converges instead of duplicating work.
"""

import errno
import fcntl
import filecmp
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from yubal_api.db.intake_ledger import FilingRefused, IntakeLedger, VerifiedAudio
from yubal_api.services.filing_preview import preview_filing
from yubal_api.services.intake_worker import IntakeWorker, verify_audio


class FileConflict(FilingRefused):
    """The library root already holds something at the proposed path."""


class AlreadyFiled(FilingRefused):
    """The source is recorded as filed; use read-back instead of re-filing."""


@dataclass(frozen=True)
class FiledAudio:
    video_id: str
    audio_path: Path
    sidecars: tuple[Path, ...]


@contextmanager
def _filing_lock(ledger: IntakeLedger) -> Iterator[None]:
    """Serialize filing plans and recovery across CLI/API processes."""
    database = ledger.engine.url.database
    if not database or database == ":memory:":
        raise FilingRefused("Filing requires a durable local database")
    with Path(database).with_suffix(".filing.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _move_no_clobber(source: Path, destination: Path) -> None:
    """Publish a complete file atomically, then remove the source.

    link(2) is an atomic no-replace operation, including for dangling symlinks.
    Cross-filesystem copies are fsynced privately in the destination directory
    before publication. A crash can leave both paths; reconciliation checks exact
    bytes before removing the source. Never fall back to an overwriting rename.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        raise FileConflict("Refusing to file a source symlink")
    try:
        with source.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(source, destination, follow_symlinks=False)
    except FileExistsError as error:
        raise FileConflict(f"Destination already exists: {destination}") from error
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".yubal-filing-", dir=destination.parent, delete=False
            ) as output:
                temporary = Path(output.name)
                with source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, output)
                output.flush()
                shutil.copystat(source, temporary)
                os.fsync(output.fileno())
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError as conflict:
                raise FileConflict(
                    f"Destination already exists: {destination}"
                ) from conflict
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    _sync_directory(destination.parent)
    source.unlink()
    _sync_directory(source.parent)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _finish_sidecars(sidecars: list[tuple[str, str]]) -> None:
    for source_name, target_name in sidecars:
        source, target = Path(source_name), Path(target_name)
        if not source.is_file():
            if not target.is_file() or target.is_symlink():
                raise FileConflict(f"Planned sidecar is missing: {source}")
            continue
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not filecmp.cmp(source, target, shallow=False):
                raise FileConflict(f"Sidecar destination changed: {target}")
            _sync_directory(target.parent)
            source.unlink()
            _sync_directory(source.parent)
        else:
            _move_no_clobber(source, target)


def file_verified_audio(
    ledger: IntakeLedger,
    video_id: str,
    music_root: Path,
    *,
    after_audio_move: Callable[[Path], None] | None = None,
) -> FiledAudio:
    """File one reviewed, verified, tagged source; raise rather than overwrite."""
    with _filing_lock(ledger):
        return _file_verified_audio_locked(
            ledger, video_id, music_root, after_audio_move=after_audio_move
        )


def _file_verified_audio_locked(
    ledger: IntakeLedger,
    video_id: str,
    music_root: Path,
    *,
    after_audio_move: Callable[[Path], None] | None = None,
) -> FiledAudio:
    if ledger.filing_plan(video_id) is not None:
        # A previous attempt may have died between the plan and the ledger write.
        _reconcile_filing_locked(ledger)
    if ledger.downstream_state(video_id) == "filed":
        raise AlreadyFiled(f"Source is already filed at {ledger.final_path(video_id)}")
    preview = preview_filing(ledger, video_id, music_root)
    if preview.conflicts:
        raise FileConflict("; ".join(preview.conflicts))
    source_audio = verify_audio(preview.source)
    if source_audio.pcm_sha256 is None:
        raise FileConflict("Verified audio lost its decoded-audio fingerprint")
    ledger.plan_filing(video_id, preview.destination, list(preview.sidecars))
    _move_no_clobber(preview.source, preview.destination)
    if after_audio_move is not None:
        # Test hook for the crash window between the audio move and the ledger write.
        after_audio_move(preview.destination)
    moved: list[Path] = []
    for sidecar_source, sidecar_target in preview.sidecars:
        _move_no_clobber(sidecar_source, sidecar_target)
        moved.append(sidecar_target)
    filed_audio = verify_audio(preview.destination)
    if filed_audio.pcm_sha256 != source_audio.pcm_sha256:
        raise FileConflict("Filed audio does not decode to the verified recording")
    ledger.record_filed(video_id, filed_audio, preview.destination)
    return FiledAudio(video_id, preview.destination, tuple(moved))


def _matches_recording(path: Path, pcm_sha256: str) -> VerifiedAudio | None:
    if not path.is_file():
        return None
    verified = verify_audio(path)
    if verified.pcm_sha256 != pcm_sha256:
        return None
    return verified


def reconcile_filing(ledger: IntakeLedger) -> int:
    """Finish, discard or report interrupted and lost filed audio.

    Returns the number of sources whose downstream status changed.
    """
    with _filing_lock(ledger):
        return _reconcile_filing_locked(ledger)


def recover_filing_and_downloads(worker: IntakeWorker) -> int:
    """Keep CLI filing out of the entire startup missing-output scan."""
    with _filing_lock(worker.ledger):
        changes = _reconcile_filing_locked(worker.ledger)
        worker.recover_startup()
        return changes


def _reconcile_filing_locked(ledger: IntakeLedger) -> int:
    changes = 0
    for candidate in ledger.pending_filings():
        if candidate.status == "filed":
            # Filed audio must still decode to the verified recording, not merely
            # occupy its recorded path.
            if _matches_recording(candidate.destination, candidate.pcm_sha256) is None:
                ledger.mark_output_missing(
                    candidate.video_id,
                    f"Filed audio is missing or replaced: {candidate.destination}",
                )
                changes += 1
            continue
        if candidate.source_path.is_file():
            # Publication succeeded but the process died before unlinking the
            # source. PCM alone is not enough: preserve all metadata bytes too.
            if (
                candidate.destination.is_file()
                and not candidate.destination.is_symlink()
            ):
                original = verify_audio(candidate.source_path)
                recovered = _matches_recording(
                    candidate.destination, candidate.pcm_sha256
                )
                if recovered is not None and recovered.sha256 == original.sha256:
                    _sync_directory(candidate.destination.parent)
                    candidate.source_path.unlink()
                    _sync_directory(candidate.source_path.parent)
                    _finish_sidecars(candidate.sidecars)
                    ledger.record_filed(
                        candidate.video_id, recovered, candidate.destination
                    )
                    changes += 1
                    continue
            if ledger.filing_plan(candidate.video_id) is not None:
                # The move never started; the audio can still be filed.
                ledger.clear_filing_plan(candidate.video_id, candidate.destination)
            continue
        recovered = _matches_recording(candidate.destination, candidate.pcm_sha256)
        if recovered is not None:
            _finish_sidecars(candidate.sidecars)
            ledger.record_filed(candidate.video_id, recovered, candidate.destination)
            changes += 1
        elif ledger.mark_output_missing(
            candidate.video_id, f"Recorded audio is absent: {candidate.destination}"
        ):
            changes += 1
    return changes
