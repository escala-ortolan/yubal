"""Owner registration of stable local audio, without invented video IDs."""

import time
from pathlib import Path

from yubal_api.db.intake_ledger import IntakeLedger
from yubal_api.services.filing import _filing_lock
from yubal_api.services.intake_worker import verify_audio


def register_manual_paths(
    ledger: IntakeLedger, root: Path, paths: list[Path], *, settle_seconds: int = 300
) -> list[dict[str, str]]:
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Invalid intake root")
    if len(paths) != len(set(paths)) or not paths:
        raise ValueError("Explicit unique arrival paths required")
    with _filing_lock(ledger):
        for path in paths:
            path = path.absolute()
            if not path.is_relative_to(root) or any(
                p.is_symlink() for p in [path, *path.parents]
            ):
                raise ValueError(
                    "Manual audio must remain inside intake without symlinks"
                )
            if any(p.startswith((".", "_")) for p in path.relative_to(root).parts):
                raise ValueError("Hidden/inflight path is not a manual arrival")
            stat = path.stat()
            before = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if time.time() - stat.st_mtime < settle_seconds:
                raise ValueError("Manual arrival has not settled/stabilized")
            try:
                audio = verify_audio(path)
            except ValueError as exc:
                raise ValueError(f"Manual arrival held: {path}: {exc}") from exc
            stat = path.stat()
            if before != (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns):
                raise ValueError("Manual audio changed while being verified")
            ledger.register_manual_audio(audio)
        wanted = {str(p.absolute()) for p in paths}
        return [item for item in ledger.tagger_work(root) if item["path"] in wanted]
