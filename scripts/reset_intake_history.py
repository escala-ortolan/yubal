"""Owner-only, offline reset of download history, preserving devices/schedules.

Stop the intake service before --apply. The old music files are never touched.
"""

import argparse
import fcntl
import os
import sqlite3
from pathlib import Path

from yubal_api.db.local_disk import require_local_sqlite
from yubal_api.settings import get_settings

TABLES = (
    "intake_controls",
    "intake_items",
    "download_attempts",
    "downstream_stages",
    "source_aliases",
    "source_tracks",
    "intake_actions",
    "intakes",
    "intake_rate_events",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--backup",
        type=Path,
        help="Unique local SQLite backup path; required for --apply",
    )
    args = parser.parse_args()
    if args.apply != bool(args.backup):
        parser.error("--apply requires --backup and --backup requires --apply")
    settings = get_settings()
    if not settings.intake_only:
        parser.error("Only available in fresh intake mode")
    require_local_sqlite(settings.db_path)
    lock_path = settings.db_path.parent / "intake-worker.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            parser.error("Worker is still active; stop the intake service first")
            raise AssertionError from exc
        with sqlite3.connect(settings.db_path) as database:
            database.execute("PRAGMA foreign_keys=ON")
            version = database.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
            if version != ("d42b6c1a0004",):
                parser.error("Expected current intake schema revision d42b6c1a0004")
            if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                parser.error("Source SQLite integrity check failed")
            counts = {
                table: database.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (*TABLES, "intake_devices", "intake_schedules")
            }
            print("Before:", counts)
            if not args.apply:
                print(
                    "Dry run only. Stop the server and pass --apply "
                    "--backup /local/unique.db"
                )
                return
            backup: Path = args.backup
            backup.parent.mkdir(parents=True, exist_ok=True)
            require_local_sqlite(backup)
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            try:
                with sqlite3.connect(backup) as saved:
                    database.backup(saved)
                    if saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("Backup integrity check failed")
                    if (
                        saved.execute(
                            "SELECT version_num FROM alembic_version"
                        ).fetchone()
                        != version
                    ):
                        raise RuntimeError("Backup revision differs from source")
                database.execute("BEGIN IMMEDIATE")
                for table in TABLES:
                    database.execute(f"DELETE FROM {table}")
                database.execute(
                    "UPDATE intake_schedules SET run_id=NULL, pending_payload=NULL, "
                    "last_run=NULL, last_intake_id=NULL, last_error=NULL"
                )
                database.commit()
            except Exception:
                database.rollback()
                raise
            after = {
                table: database.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (*TABLES, "intake_devices", "intake_schedules")
            }
            if any(after[table] for table in TABLES) or any(
                after[table] != counts[table]
                for table in ("intake_devices", "intake_schedules")
            ):
                raise RuntimeError(
                    "Reset did not preserve devices/schedules or empty history"
                )
            print("After:", after)
            print("Local backup:", backup)


if __name__ == "__main__":
    main()
