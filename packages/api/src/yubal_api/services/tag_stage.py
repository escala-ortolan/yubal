"""Observe external/manual tagging; never invoke external tagger from Yubal."""

from mutagen import File as MutagenFile

from yubal_api.db.intake_ledger import IntakeLedger
from yubal_api.services.intake_worker import verify_audio


def observe_manual_tags(ledger: IntakeLedger, video_id: str) -> str:
    """Owner-triggered read-back after an external external tagger folder scan."""
    path, expected_pcm = ledger.tagging_candidate(video_id)
    audio = verify_audio(path)
    if audio.pcm_sha256 != expected_pcm:
        raise ValueError("Tagged file has different audio than the download")
    tags = MutagenFile(path, easy=True)
    values = tags.tags if tags is not None else None
    if not values:
        raise ValueError("No inspected title/artist tags")
    title = values.get("title", [""])[0].strip()
    artist = (
        values.get("albumartist", [""])[0].strip()
        or values.get("artist", [""])[0].strip()
    )
    if not title or not artist:
        raise ValueError("Missing inspected title/artist tags")
    return ledger.record_tagged(
        video_id, audio, f"manual-external-scan:{artist} / {title}"
    )
