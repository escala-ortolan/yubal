"""Read-only flat-layout filing preview; never mutates a music library."""

import re
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile
from yubal.utils.filename import MAX_PATH_COMPONENT_BYTES, clean_filename

from yubal_api.db.intake_ledger import IntakeLedger
from yubal_api.services.intake_worker import verify_audio

SIDECAR_SUFFIXES = (".lrc", ".jpg", ".png", ".webp")


@dataclass(frozen=True)
class FilingPreview:
    source: Path
    destination: Path
    sidecars: tuple[tuple[Path, Path], ...]
    conflicts: tuple[str, ...]


def _safe_component(value: str, *, max_bytes: int) -> str:
    result = clean_filename(value).strip(" .")
    result = result.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    result = result.rstrip(" .")
    if result in {"", ".", ".."}:
        raise ValueError("Tag does not produce a safe library component")
    return result


def preview_filing(
    ledger: IntakeLedger, video_id: str, music_root: Path
) -> FilingPreview:
    """Propose Main Artist/Title with sidecars; fail closed on collisions."""
    if ledger.downstream_state(video_id) != "tagged":
        raise ValueError("Review tags before proposing a library path")
    source, pcm_sha = ledger.tagging_candidate(video_id)
    if verify_audio(source).pcm_sha256 != pcm_sha:
        raise ValueError("Audio is not the verified downloaded recording")
    audio = MutagenFile(source, easy=True)
    tags = audio.tags if audio is not None else None
    if not tags:
        raise ValueError("Tagged audio has no metadata")
    title = tags.get("title", [""])[0].strip()
    artist = (
        tags.get("albumartist", [""])[0].strip() or tags.get("artist", [""])[0].strip()
    )
    # Only explicit guest-credit markers are removed. A genuine "&" stays.
    main_artist = re.sub(
        r"\s+(?:feat\.?|ft\.?|featuring)\s+.+$", "", artist, flags=re.I
    )
    root = music_root.resolve()
    folder = _safe_component(main_artist, max_bytes=MAX_PATH_COMPONENT_BYTES)
    filename = _safe_component(title, max_bytes=MAX_PATH_COMPONENT_BYTES - 4)
    destination = root / folder / f"{filename}.m4a"
    sidecars = tuple(
        (source.with_suffix(suffix), destination.with_suffix(suffix))
        for suffix in SIDECAR_SUFFIXES
        if source.with_suffix(suffix).is_file()
    )
    conflicts: list[str] = []
    if root.exists():
        for existing in root.iterdir():
            if (
                existing.name.casefold() == folder.casefold()
                and existing.name != folder
            ):
                conflicts.append(f"Artist folder differs only by case: {existing}")
    if destination.parent.exists():
        for target in (destination, *(dst for _, dst in sidecars)):
            for existing in target.parent.iterdir():
                if existing.name.casefold() == target.name.casefold():
                    conflicts.append(f"Destination already exists: {existing}")
    return FilingPreview(source, destination, sidecars, tuple(conflicts))
