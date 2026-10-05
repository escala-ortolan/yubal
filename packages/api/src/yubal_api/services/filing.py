"""Move already-tagged verified audio into a flat library, recoverably.

Filing only ever writes into the music root the caller names, never touches an
existing file there, and records its intent in the ledger before the first move
so an interrupted filing converges instead of duplicating work.
"""

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from yubal_api.db.intake_ledger import FilingRefused, IntakeLedger, VerifiedAudio
from yubal_api.services.filing_preview import preview_filing
from yubal_api.services.intake_worker import verify_audio


class FileConflict(FilingRefused):
    """The library root already holds something at the proposed path."""


class AlreadyFiled(FilingRefused):
    """The source is recorded as filed; use read-back instead of re-filing."""


@dataclass(frozen=True)
class FiledAudio:
    video_id: str
    audio_path: Path
    sidecars: tuple[Path, ...]


def _move_no_clobber(source: Path, destination: Path) -> None:
    """Move a file without ever overwriting an existing destination."""
    if destination.exists():
        raise FileConflict(f"Destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
    except OSError as error:
        if error.errno != 18:  # EXDEV: rename cannot cross filesystems.
            raise
        shutil.move(str(source), str(destination))


def file_verified_audio(
    ledger: IntakeLedger,
    video_id: str,
    music_root: Path,
    *,
    after_audio_move: Callable[[Path], None] | None = None,
) -> FiledAudio:
    """File one reviewed, verified, tagged source; raise rather than overwrite."""
    if ledger.filing_plan(video_id) is not None:
        # A previous attempt may have died between the plan and the ledger write.
        reconcile_filing(ledger)
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
            if ledger.filing_plan(candidate.video_id) is not None:
                # The move never started; the audio can still be filed.
                ledger.clear_filing_plan(candidate.video_id, candidate.destination)
            continue
        recovered = _matches_recording(candidate.destination, candidate.pcm_sha256)
        if recovered is not None:
            for sidecar_source, sidecar_target in candidate.sidecars:
                source = Path(sidecar_source)
                if source.is_file() and not Path(sidecar_target).exists():
                    _move_no_clobber(source, Path(sidecar_target))
            ledger.record_filed(candidate.video_id, recovered, candidate.destination)
            changes += 1
        elif ledger.mark_output_missing(
            candidate.video_id, f"Recorded audio is absent: {candidate.destination}"
        ):
            changes += 1
    return changes
