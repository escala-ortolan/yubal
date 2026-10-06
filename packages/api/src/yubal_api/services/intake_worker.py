"""Single-owner staging downloader with real media validation and restart recovery."""

import hashlib
import json
import os
import re
import subprocess
import unicodedata
from collections.abc import Callable
from pathlib import Path

from yubal import AudioCodec, DownloadConfig
from yubal.services.download_service import YTDLPDownloader

from yubal_api.db.intake_ledger import IntakeLedger, VerifiedAudio

Transfer = Callable[[str, Path], Path]


def _metadata_from_filename(filename: str) -> tuple[str | None, str | None]:
    stem = Path(filename).name.removesuffix(".m4a")
    stem = re.sub(r" \[[A-Za-z0-9_-]{11}(?:-[0-9a-f]{8})?\]$", "", stem)
    if " - " not in stem:
        return (stem[:500] or None), None
    artist, title = stem.split(" - ", 1)
    return (title[:500] or None), (artist[:500] or None)


def sanitize_filename(filename: str, video_id: str) -> str:
    """Turn yt-dlp's server-side metadata basename into one safe m4a filename."""
    stem = filename[:-4] if filename.lower().endswith(".m4a") else filename
    stem = "".join(
        " " if char in "/\\" or unicodedata.category(char).startswith("C") else char
        for char in stem
    )
    stem = re.sub(r"\s+", " ", stem).strip(" .")[:150].rstrip(" .")
    while len(stem.encode("utf-8")) > 235:
        stem = stem[:-1].rstrip(" .")
    return f"{stem or video_id}.m4a"


def verify_audio(path: Path) -> VerifiedAudio:
    """Decode and hash m4a; file existence alone is insufficient."""
    if not path.is_file() or path.suffix.lower() != ".m4a" or path.stat().st_size == 0:
        raise ValueError("Missing or empty m4a audio")
    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        info = json.loads(probe.stdout)
        streams = [
            stream for stream in info["streams"] if stream.get("codec_type") == "audio"
        ]
        duration = float(info["format"].get("duration", 0))
        if (
            len(streams) != 1
            or streams[0]["codec_name"] not in {"aac", "alac"}
            or "mp4" not in info["format"]["format_name"]
            or duration <= 0
        ):
            raise ValueError("Invalid m4a audio stream")
        decoded = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-c:a",
                "pcm_s16le",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-f",
                "hash",
                "-hash",
                "sha256",
                "-",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        pcm_sha = decoded.stdout.strip().removeprefix("SHA256=")
        if len(pcm_sha) != 64 or any(c not in "0123456789abcdef" for c in pcm_sha):
            raise ValueError("Missing decoded-audio fingerprint")
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        raise ValueError("Invalid or undecodable audio") from exc
    digest = hashlib.sha256()
    size = path.stat().st_size
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if path.stat().st_size != size:
        raise ValueError("Audio changed during verification")
    return VerifiedAudio(
        str(path),
        size,
        digest.hexdigest(),
        duration,
        str(streams[0]["codec_name"]),
        pcm_sha,
    )


class IntakeWorker:
    """Only run under an exclusive process lock; recover before new claims."""

    def __init__(
        self, ledger: IntakeLedger, staging_root: Path, transfer: Transfer | None = None
    ) -> None:
        self.ledger = ledger
        self.staging_root = staging_root.resolve()
        self._download_metadata: dict[str, str | None] = {}
        if transfer is None:
            downloader = YTDLPDownloader(
                DownloadConfig(
                    base_path=self.staging_root,
                    codec=AudioCodec.M4A,
                    fetch_lyrics=False,
                )
            )

            def transfer_with_metadata(video_id: str, output: Path) -> Path:
                return downloader.download(
                    video_id,
                    output,
                    metadata_callback=lambda title, artist: (
                        self._download_metadata.update(
                            {"title": title, "artist": artist}
                        )
                    ),
                )

            self.transfer: Transfer = transfer_with_metadata
        else:
            self.transfer = transfer

    def recover_startup(self) -> None:
        """After acquiring exclusive ownership, reconcile every unfinished attempt."""
        for claim in self.ledger.inflight():
            try:
                output = self.ledger.planned_output(claim)
                if output.is_relative_to(self.staging_root) and not output.is_symlink():
                    title, artist = _metadata_from_filename(output.name)
                    self.ledger.complete(
                        claim,
                        verify_audio(output),
                        title=title,
                        artist=artist,
                    )
                    for temporary in self.staging_root.glob(
                        f".yubal-inflight-{claim.attempt_id}-*.m4a"
                    ):
                        if not temporary.is_symlink() and temporary.samefile(output):
                            temporary.unlink()
                else:
                    self.ledger.fail(claim, "Invalid staging path")
            except (ValueError, OSError):
                self.ledger.fail(claim, "No verified audio at recovery")
        self.ledger.reconcile_missing_outputs(verify_audio)

    def run_once(self) -> bool:
        """Process at most one pending source; never auto-retry failed audio."""
        video_id = self.ledger.next_pending()
        if video_id is None:
            return False
        claim = self.ledger.claim(video_id, staging_root=self.staging_root)
        if claim is None:
            return False
        try:
            self._download_metadata.clear()
            self.staging_root.mkdir(parents=True, exist_ok=True)
            prefix = f".yubal-inflight-{claim.attempt_id}-"
            template = self.staging_root / (
                prefix + "%(artist,uploader|Unknown Artist)s - %(title)s"
            )
            actual = self.transfer(claim.video_id, template)
            if (
                actual.parent.resolve() != self.staging_root
                or actual.is_symlink()
                or not actual.name.startswith(prefix)
                or actual.suffix.lower() != ".m4a"
            ):
                raise ValueError("Downloader output escaped its flat staging template")
            verify_audio(actual)
            name = sanitize_filename(actual.name[len(prefix) :], claim.video_id)
            stem = name[:-4]
            choices = [
                name,
                f"{stem} [{claim.video_id}].m4a",
                f"{stem} [{claim.video_id}-{claim.attempt_id[:8]}].m4a",
            ]
            for choice in choices:
                flat = self.staging_root / choice
                # Record the exact location before publishing so a kill between
                # hardlink and ledger completion can recover without downloading.
                self.ledger.plan_actual_output(claim, flat)
                try:
                    os.link(actual, flat, follow_symlinks=False)
                except FileExistsError:
                    continue
                fallback_title, fallback_artist = _metadata_from_filename(
                    actual.name[len(prefix) :]
                )
                self.ledger.complete(
                    claim,
                    verify_audio(flat),
                    title=self._download_metadata.get("title") or fallback_title,
                    artist=self._download_metadata.get("artist") or fallback_artist,
                )
                actual.unlink()
                break
            else:
                raise ValueError("No collision-free staging filename")
        except Exception as exc:
            # Do not log yt-dlp errors or potentially sensitive URL/cookie contents.
            self.ledger.fail(
                claim, f"Download/verification failed: {type(exc).__name__}"
            )
        return True
