"""Consistent SQLite copy to a new local path; never overwrite a database."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from yubal_api.db.local_disk import require_local_sqlite


def backup_intake_db(source: Path, destination: Path) -> None:
    """Use SQLite's online backup API, then integrity-check the result."""
    require_local_sqlite(source)
    require_local_sqlite(destination)
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    with closing(
        sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    ) as reader:
        with closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer)
            if writer.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("Backup failed SQLite integrity check")
