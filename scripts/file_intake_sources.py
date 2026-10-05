"""File reviewed, tagged, verified audio into a flat library, or reconcile it.

Read-only by default. `--apply` moves one reviewed source at a time and refuses
to overwrite anything already in the music root. `--reconcile` finishes or
reports an interrupted filing and never transfers audio.
"""

import argparse
from pathlib import Path

from yubal_api.api.app import run_migrations
from yubal_api.db.engine import create_db_engine
from yubal_api.db.intake_ledger import FilingRefused, IntakeLedger
from yubal_api.db.local_disk import require_local_sqlite
from yubal_api.services.filing import file_verified_audio, reconcile_filing
from yubal_api.services.filing_preview import preview_filing
from yubal_api.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id", nargs="?", help="Source video ID to file")
    parser.add_argument(
        "music_root", nargs="?", type=Path, help="Destination Music folder"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Move the audio instead of previewing"
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Finish or report interrupted and lost filed audio, then exit",
    )
    args = parser.parse_args()
    settings = get_settings()
    if not settings.intake_only:
        parser.error("Set YUBAL_INTAKE_ONLY=true")
    require_local_sqlite(settings.db_path)
    run_migrations()
    engine = create_db_engine(settings.db_path)
    ledger = IntakeLedger(engine)
    try:
        if args.reconcile:
            print(f"reconciled sources: {reconcile_filing(ledger)}")
            return
        if not args.video_id or args.music_root is None:
            parser.error("video_id and music_root are required unless --reconcile")
        if not args.apply:
            proposal = preview_filing(ledger, args.video_id, args.music_root)
            print(f"audio: {proposal.source} -> {proposal.destination}")
            for source, destination in proposal.sidecars:
                print(f"sidecar: {source} -> {destination}")
            for conflict in proposal.conflicts:
                print(f"REVIEW: {conflict}")
            print("dry run: pass --apply to move")
            return
        filed = file_verified_audio(ledger, args.video_id, args.music_root)
        print(f"filed: {filed.audio_path}")
        for sidecar in filed.sidecars:
            print(f"filed sidecar: {sidecar}")
    except FilingRefused as error:
        parser.exit(1, f"refused: {error}\n")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
