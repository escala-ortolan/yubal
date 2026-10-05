"""Show a read-only proposed flat file layout and every detected collision."""

import argparse
from pathlib import Path

from yubal_api.api.app import run_migrations
from yubal_api.db.engine import create_db_engine
from yubal_api.db.intake_ledger import IntakeLedger
from yubal_api.db.local_disk import require_local_sqlite
from yubal_api.services.filing_preview import preview_filing
from yubal_api.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id", help="Source video ID")
    parser.add_argument("music_root", type=Path, help="Proposed Music folder")
    args = parser.parse_args()
    settings = get_settings()
    if not settings.intake_only:
        parser.error("Set YUBAL_INTAKE_ONLY=true")
    require_local_sqlite(settings.db_path)
    run_migrations()
    engine = create_db_engine(settings.db_path)
    try:
        proposal = preview_filing(IntakeLedger(engine), args.video_id, args.music_root)
        print(f"audio: {proposal.source} -> {proposal.destination}")
        for source, destination in proposal.sidecars:
            print(f"sidecar: {source} -> {destination}")
        for conflict in proposal.conflicts:
            print(f"REVIEW: {conflict}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
