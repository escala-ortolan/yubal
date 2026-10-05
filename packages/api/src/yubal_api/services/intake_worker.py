"""Single-owner staging downloader with real media validation and restart recovery."""

import hashlib
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from yubal import AudioCodec, DownloadConfig
from yubal.services.download_service import YTDLPDownloader

from yubal_api.db.intake_ledger import IntakeLedger, VerifiedAudio

Transfer = Callable[[str, Path], Path]


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
        if transfer is None:
            downloader = YTDLPDownloader(
                DownloadConfig(
                    base_path=self.staging_root,
                    codec=AudioCodec.M4A,
                    fetch_lyrics=False,
                )
            )
            self.transfer: Transfer = lambda video_id, output: downloader.download(
                video_id, output
            )
        else:
            self.transfer = transfer

    def recover_startup(self) -> None:
        """After acquiring exclusive ownership, reconcile every unfinished attempt."""
        for claim in self.ledger.inflight():
            try:
                output = self.ledger.planned_output(claim)
                if output.is_relative_to(self.staging_root):
                    self.ledger.complete(claim, verify_audio(output))
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
            output = self.ledger.planned_output(claim)
            output.parent.mkdir(parents=True, exist_ok=True)
            actual = self.transfer(claim.video_id, output.with_suffix(""))
            if actual != output:
                raise ValueError("Downloader did not return planned m4a path")
            self.ledger.complete(claim, verify_audio(actual))
        except Exception as exc:
            # Do not log yt-dlp errors or potentially sensitive URL/cookie contents.
            self.ledger.fail(
                claim, f"Download/verification failed: {type(exc).__name__}"
            )
        return True
