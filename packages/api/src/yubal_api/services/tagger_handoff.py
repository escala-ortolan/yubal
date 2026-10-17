"""Opt-in owner-controlled filing policy for the external in-place tagger.

No network completion callback or tagger-side database access. Legacy filing stays
no-clobber. Exact audio-path replacement first preserves the old file locally;
sidecar/case/accent conflicts and uncertain quality are held, never overwritten.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile

from yubal_api.db.intake_ledger import SOURCE_ID, FilingRefused, IntakeLedger
from yubal_api.db.local_disk import require_local_sqlite
from yubal_api.services.filing import (
    _file_verified_audio_locked,
    _filing_lock,
    _reconcile_filing_locked,
    _sync_directory,
)
from yubal_api.services.filing_preview import SIDECAR_SUFFIXES, FilingPreview
from yubal_api.services.intake_worker import verify_audio

DEFAULT_DUOS = ("João Carreiro & Capataz", "Bruno & Marrone")


def folded(value: str) -> str:
    text = "".join(
        c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)
    )
    return re.sub(r"\s+", " ", text.casefold()).strip().rstrip(" .")


def canonical_duo(value: str, duos: Sequence[str] = DEFAULT_DUOS) -> str:
    def identity(text: str) -> str:
        return re.sub(r"\s+(?:and|e|&)\s+", " & ", folded(text))

    for duo in duos:
        if " & " not in duo:
            raise ValueError("Canonical duo must contain ' & '")
        if identity(value) == identity(duo):
            return duo
    return value


def split_artist_values(
    values: Sequence[str], duos: Sequence[str] = DEFAULT_DUOS
) -> list[str]:
    """Explicit credit boundaries only; never split &, commas or bare AC/DC slashes."""
    result = []
    seen = set()
    for value in values:
        for part in re.split(r"\s*;\s*|\s+/\s+|[\r\n]+", value):
            artist = canonical_duo(part.strip(), duos)
            identity = folded(artist)
            if identity and identity not in seen:
                result.append(artist)
                seen.add(identity)
    return result


def component(value: str, limit: int = 140) -> str:
    value = "".join(
        c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)
    )
    value = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "-", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(" .").lower()
    value = re.sub(r"[^\W\d_]", lambda m: m[0].upper(), value, count=1)
    if not value or value in {".", ".."}:
        raise ValueError("No safe artist/title component")
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I):
        value = "_" + value
    return value.encode()[:limit].decode(errors="ignore").rstrip(" .")


def _signature(path: Path) -> list[int]:
    if path.is_symlink() or not path.is_file():
        raise FilingRefused(f"Not a regular audio file: {path}")
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _sha(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _sync_directory(path.parent)


def quality_winner(incoming: dict, existing: dict) -> str:
    """No file-size heuristic; incomparable encodings/mixes require review."""
    if (
        incoming.get("channels")
        and existing.get("channels")
        and incoming["channels"] != existing["channels"]
    ):
        return "review"
    lossless = {"alac", "flac", "wavpack", "ape", "pcm_s16le", "pcm_s24le", "pcm_s32le"}
    a, b = incoming.get("codec"), existing.get("codec")
    if (a in lossless) != (b in lossless):
        return "incoming" if a in lossless else "existing"
    if a in lossless and b in lossless:
        fields = ("bits", "sample_rate")
    elif a and a == b and incoming.get("bitrate") and existing.get("bitrate"):
        fields = ("bitrate", "sample_rate")
    else:
        return "review"
    left = [incoming.get(k, 0) for k in fields]
    right = [existing.get(k, 0) for k in fields]
    if all(a >= b for a, b in zip(left, right, strict=True)) and left != right:
        return "incoming"
    if all(a <= b for a, b in zip(left, right, strict=True)):
        return "existing"
    return "review"


def _quality(path: Path) -> dict:
    output = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,bit_rate,sample_rate,bits_per_raw_sample,bits_per_sample,channels",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    stream = json.loads(output.stdout)["streams"][0]

    def number(name: str) -> int:
        try:
            return int(stream.get(name, 0))
        except (TypeError, ValueError):
            return 0

    return {
        "codec": stream["codec_name"],
        "bitrate": number("bit_rate"),
        "sample_rate": number("sample_rate"),
        "channels": number("channels"),
        "bits": number("bits_per_raw_sample") or number("bits_per_sample"),
    }


def _values(tags: Any, name: str) -> list[str]:
    values = tags.get(name, [])
    if hasattr(values, "text"):
        values = values.text
    if not isinstance(values, (tuple, list)):
        values = [values]
    return [v.decode("utf8") if isinstance(v, bytes) else str(v) for v in values]


def tag_credit(
    source: Path, duos: Sequence[str] = DEFAULT_DUOS
) -> tuple[str, list[str], str]:
    """Read entity credits across native tag formats, including combined values."""
    audio = MutagenFile(source, easy=True)
    raw = MutagenFile(source)
    if audio is None or raw is None or not audio.tags:
        raise FilingRefused("No readable artist/title tags")

    def get(*names: str) -> list[str]:
        for name in names:
            for tags in (audio.tags, raw.tags or {}):
                values = _values(tags, name)
                if any(v.strip() for v in values):
                    return values
        return []

    artists = split_artist_values(get("artist", "TPE1", "©ART", "Author"), duos)
    albums = split_artist_values(
        get("albumartist", "TPE2", "aART", "WM/AlbumArtist"), duos
    )
    structured = split_artist_values(
        get("----:com.apple.iTunes:ARTISTS", "TXXX:ARTISTS", "artists", "ARTISTS"), duos
    )
    album_entities = split_artist_values(
        get(
            "----:com.apple.iTunes:ALBUM_ARTISTS",
            "----:com.apple.iTunes:ALBUMARTISTS",
            "TXXX:ALBUM_ARTISTS",
            "TXXX:ALBUMARTISTS",
            "albumartists",
            "ALBUM_ARTISTS",
        ),
        duos,
    )
    credited = structured or artists or albums
    # A whole album-artist entity is stronger group evidence than separate member
    # credits. Rejoin only an exact leading member set, never arbitrary '&' names.
    album_group = ""
    if len(albums) == 1:
        members = re.split(r"\s+(?:&|and|e)\s+", albums[0], flags=re.I)
        if (
            len(members) > 1
            and len(credited) >= len(members)
            and {folded(v) for v in members}
            == {folded(v) for v in credited[: len(members)]}
        ):
            album_group = albums[0]
            credited = [album_group, *credited[len(members) :]]
    main = (structured or album_entities or albums or artists or [""])[0]
    if album_group:
        main = album_group
    # Registered groups remain whole even if a tagger supplies individual members.
    if len(artists) == 1 and artists[0] in duos:
        credited = artists
    if albums and albums[0] in duos:
        main = albums[0]
    elif artists and artists[0] in duos:
        main = artists[0]
    main = canonical_duo(
        re.sub(r"\s+(?:feat\.?|ft\.?|featuring)\s+.+$", "", main, flags=re.I), duos
    )
    if folded(main) in {"various artists", "unknown artist", "unknown"}:
        main = artists[0] if artists else ""
    title = (get("title", "TIT2", "©nam", "Title") or [""])[0].strip()
    if not main or not title or not credited:
        raise FilingRefused("Missing readable artist/title; leave the source in intake")
    return main, credited, title


def preview_reviewed(
    ledger: IntakeLedger,
    video_id: str,
    music_root: Path,
    duos: Sequence[str] = DEFAULT_DUOS,
) -> dict:
    if ledger.downstream_state(video_id) != "tagged":
        raise FilingRefused("Observe tags through Yubal before reviewing filing")
    source, expected_pcm = ledger.tagging_candidate(video_id)
    sig = _signature(source)
    verified = verify_audio(source)
    if verified.pcm_sha256 != expected_pcm or _signature(source) != sig:
        raise FilingRefused("Source is no longer the verified downloaded recording")
    main, credited, title = tag_credit(source, duos)
    folder = component(main)
    if folder.startswith((".", "_")) or folded(folder) in {
        "duplicate",
        "downloads",
        "intake-staging",
    }:
        raise FilingRefused("Artist conflicts with a reserved folder")
    root = music_root.resolve()
    budget = min(180, 220 - len(folder.encode()) - len(source.suffix) - 1)
    credit, song = component(", ".join(credited), 200), component(title, 200)
    if len((credit + " - " + song).encode()) > budget:
        title_budget = min(len(song.encode()), max(32, budget // 2))
        artist_budget = min(len(credit.encode()), budget - 3 - title_budget)
        credit = credit.encode()[:artist_budget].decode(errors="ignore").rstrip(" .")
        song = (
            song.encode()[: budget - 3 - artist_budget]
            .decode(errors="ignore")
            .rstrip(" .")
        )
    destination = root / folder / (credit + " - " + song + source.suffix.lower())
    if any(parent.is_symlink() for parent in destination.parents):
        raise FilingRefused("Symlink in destination path")
    sidecars = [
        (source.with_suffix(ext), destination.with_suffix(ext))
        for ext in SIDECAR_SUFFIXES
        if source.with_suffix(ext).is_file()
    ]
    conflicts = []
    if root.exists():
        for p in root.iterdir():
            if (
                folded(canonical_duo(p.name, duos)) == folded(folder)
                and p.name != folder
                and not (root / folder).is_dir()
            ):
                conflicts.append(
                    f"Artist alias/case folder needs a reviewed merge: {p}"
                )
    if destination.parent.exists():
        for target in [destination, *(b for _, b in sidecars)]:
            for p in target.parent.iterdir():
                if folded(p.name) == folded(target.name) and (
                    p != destination or target != destination
                ):
                    conflicts.append(f"Case/accent/sidecar conflict: {p}")
    decision = "new"
    existing = None
    if destination.exists() or destination.is_symlink():
        existing_sig = _signature(destination)
        existing = {
            "signature": existing_sig,
            "sha256": _sha(destination),
            "quality": _quality(destination),
        }
        decision = quality_winner(_quality(source), existing["quality"])
        if _signature(destination) != existing_sig:
            raise FilingRefused("Existing file changed during review")
    if _signature(source) != sig:
        raise FilingRefused("Source changed during review")
    proposal = {
        "source": str(source),
        "destination": str(destination),
        "source_signature": sig,
        "source_sha256": verified.sha256,
        "sidecars": [[str(a), str(b)] for a, b in sidecars],
        "sidecar_signatures": {str(a): _signature(a) for a, _ in sidecars},
        "conflicts": conflicts,
        "quality_decision": decision,
        "existing": existing,
    }
    proposal["token"] = hashlib.sha256(
        json.dumps(proposal, sort_keys=True).encode()
    ).hexdigest()
    return proposal


def file_reviewed(
    ledger: IntakeLedger,
    video_id: str,
    music_root: Path,
    token: str,
    *,
    allow_replace: bool = False,
    duos: Sequence[str] = DEFAULT_DUOS,
    after_archive: Callable[[Path], None] | None = None,
) -> dict:
    """Owner-only reviewed operation, recoverable by repeating this same token."""
    with _filing_lock(ledger):
        canonical = ledger.resolve(video_id)
        if not SOURCE_ID.fullmatch(canonical):
            raise FilingRefused("Invalid source identity")
        database_name = ledger.engine.url.database
        if not database_name:
            raise FilingRefused("Durable local ledger required")
        database = Path(database_name)
        require_local_sqlite(database)
        receipt_path = database.parent / "tagger-filings" / (canonical + ".json")
        receipt: dict[str, Any] | None = (
            json.loads(receipt_path.read_text()) if receipt_path.exists() else None
        )
        _reconcile_filing_locked(ledger, video_id=canonical)
        if ledger.downstream_state(canonical) == "filed":
            if not receipt or receipt["token"] != token:
                raise FilingRefused("No matching reviewed filing receipt")
            final_name = ledger.final_path(canonical)
            if not final_name:
                raise FilingRefused("Filed source has no verified final path")
            final = Path(final_name)
            receipt["state"] = "complete"
            _json(receipt_path, receipt)
            return {
                "status": "filed",
                "path": str(final),
                "backup": receipt.get("backup"),
                "receipt": str(receipt_path),
            }
        if receipt:
            if receipt["token"] != token or receipt["music_root"] != str(
                music_root.resolve()
            ):
                raise FilingRefused(
                    "Different review token/root; inspect pending owner receipt"
                )
            proposal = receipt["proposal"]
            source, pcm = ledger.tagging_candidate(canonical)
            if (
                str(source) != proposal["source"]
                or verify_audio(source).pcm_sha256 != pcm
                or _sha(source) != proposal["source_sha256"]
            ):
                raise FilingRefused("Pending reviewed source changed")
        else:
            proposal = preview_reviewed(ledger, canonical, music_root, duos)
            if proposal["token"] != token:
                raise FilingRefused("Stale review token; preview again")
            if proposal["conflicts"]:
                raise FilingRefused("; ".join(proposal["conflicts"]))
            if proposal["quality_decision"] in {"existing", "review"}:
                return {
                    "status": "held_quality",
                    "path": proposal["source"],
                    "retained": proposal["destination"],
                    "decision": proposal["quality_decision"],
                }
            if proposal["existing"] and not allow_replace:
                raise FilingRefused(
                    "Exact-path replacement needs explicit owner approval"
                )
            receipt = {
                "state": "prepared",
                "token": token,
                "music_root": str(music_root.resolve()),
                "proposal": proposal,
            }
            if proposal["existing"]:
                receipt["backup"] = str(
                    database.parent
                    / "tagger-replacements"
                    / canonical
                    / Path(proposal["destination"]).name
                )
            _json(receipt_path, receipt)
        destination = Path(proposal["destination"])
        for name, sig in proposal["sidecar_signatures"].items():
            if _signature(Path(name)) != sig:
                raise FilingRefused("Reviewed sidecar changed")
        if receipt.get("backup"):
            if not allow_replace:
                raise FilingRefused(
                    "Resume replacement requires explicit owner approval"
                )
            backup = Path(receipt["backup"])
            expected = proposal["existing"]["sha256"]
            if backup.exists():
                if backup.is_symlink() or _sha(backup) != expected:
                    raise FilingRefused("Replacement backup changed")
                if destination.exists():
                    if (
                        _signature(destination) != proposal["existing"]["signature"]
                        or _sha(destination) != expected
                    ):
                        raise FilingRefused(
                            "Concurrent destination; retain both recordings"
                        )
                    destination.unlink()
                    _sync_directory(destination.parent)
            else:
                if (
                    _signature(destination) != proposal["existing"]["signature"]
                    or _sha(destination) != expected
                ):
                    raise FilingRefused("Reviewed destination changed")
                # The archive is on local disk, music may be NFS. Verify the
                # complete durable copy BEFORE removing the library pathname.
                backup.parent.mkdir(parents=True, exist_ok=True)
                if any(p.is_symlink() for p in backup.parents):
                    raise FilingRefused("Symlink in replacement archive path")
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=backup.parent, prefix=".backup-", delete=False
                    ) as output:
                        temporary = Path(output.name)
                        with destination.open("rb") as stream:
                            shutil.copyfileobj(stream, output)
                        output.flush()
                        os.fsync(output.fileno())
                    if (
                        _sha(temporary) != expected
                        or _signature(destination) != proposal["existing"]["signature"]
                        or _sha(destination) != expected
                    ):
                        raise FilingRefused(
                            "Archive/source verification failed; original retained"
                        )
                    os.link(temporary, backup, follow_symlinks=False)
                    _sync_directory(backup.parent)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                if (
                    _sha(backup) != expected
                    or _signature(destination) != proposal["existing"]["signature"]
                ):
                    raise FilingRefused("Archive changed; original retained")
                destination.unlink()
                _sync_directory(destination.parent)
            receipt["state"] = "archived"
            _json(receipt_path, receipt)
            if after_archive:
                after_archive(backup)
        preview = FilingPreview(
            Path(proposal["source"]),
            destination,
            tuple((Path(a), Path(b)) for a, b in proposal["sidecars"]),
            (),
        )
        filed = _file_verified_audio_locked(
            ledger, canonical, music_root, reviewed_preview=preview
        )
        current_pcm = verify_audio(filed.audio_path).pcm_sha256
        for previous in ledger.pending_filings():
            if (
                previous.status == "filed"
                and previous.video_id != canonical
                and previous.destination == filed.audio_path
                and previous.pcm_sha256 != current_pcm
            ):
                ledger.mark_output_missing(
                    previous.video_id,
                    "Previous recording displaced by approved quality replacement; "
                    f"recovery archive: {receipt.get('backup')}",
                )
        receipt["state"] = "complete"
        _json(receipt_path, receipt)
        return {
            "status": "filed",
            "path": str(filed.audio_path),
            "backup": receipt.get("backup"),
            "receipt": str(receipt_path),
        }
