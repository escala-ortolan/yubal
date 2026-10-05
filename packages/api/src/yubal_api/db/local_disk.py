"""Reject NFS for the fresh intake SQLite database before migration."""

from pathlib import Path


def require_local_sqlite(db_path: Path) -> None:
    """Find the containing mount using /proc mountinfo (Linux deployment)."""
    target = db_path.resolve()
    mounts: list[tuple[int, str]] = []
    for entry in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = entry.split(" - ", 1)
        mount = Path(before.split()[4].replace("\\040", " "))
        fstype = after.split()[0]
        if target == mount or mount in target.parents:
            mounts.append((len(mount.parts), fstype))
    if not mounts:
        raise RuntimeError("Unable to determine SQLite filesystem")
    if max(mounts)[1] in {"nfs", "nfs4"}:
        raise RuntimeError("Fresh intake SQLite must be on local disk, not NFS")
