"""Owner-only confirmation after a separate external tagger folder-scanning job."""

import argparse

from yubal_api.api.app import run_migrations
from yubal_api.db.engine import create_db_engine
from yubal_api.db.intake_ledger import IntakeLedger
from yubal_api.db.local_disk import require_local_sqlite
from yubal_api.services.tag_stage import observe_manual_tags
from yubal_api.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id", help="Original source video ID")
    args = parser.parse_args()
    settings = get_settings()
    if not settings.intake_only:
        parser.error("Set YUBAL_INTAKE_ONLY=true")
    require_local_sqlite(settings.db_path)
    run_migrations()
    engine = create_db_engine(settings.db_path)
    try:
        print(observe_manual_tags(IntakeLedger(engine), args.video_id))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
