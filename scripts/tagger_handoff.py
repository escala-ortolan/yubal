"""Owner-only JSON bridge for a separate in-place tagger; no new network API.

This command, not the external tagger, opens the local ledger and verifies audio.
Legacy owner scripts retain their no-overwrite behavior. The opt-in file command
adds reviewed exact-path quality replacement with a local recovery archive.
"""

import argparse
import json
from pathlib import Path

from yubal_api.api.app import run_migrations
from yubal_api.db.engine import create_db_engine
from yubal_api.db.intake_ledger import IntakeLedger
from yubal_api.db.local_disk import require_local_sqlite
from yubal_api.services.filing import reconcile_filing
from yubal_api.services.intake_worker import verify_audio
from yubal_api.services.manual_intake import register_manual_paths
from yubal_api.services.tag_stage import observe_manual_tags
from yubal_api.services.tagger_handoff import (
    DEFAULT_DUOS,
    file_reviewed,
    preview_reviewed,
)
from yubal_api.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities")
    work = commands.add_parser("work")
    work.add_argument("--source", type=Path, required=True)
    manual = commands.add_parser("register-manual")
    manual.add_argument("--source", type=Path, required=True)
    manual.add_argument("--paths-json", required=True)
    manual.add_argument("--settle", type=int, default=300)
    observe = commands.add_parser("observe")
    observe.add_argument("video_id")
    verify = commands.add_parser("verify")
    verify.add_argument("video_id")
    commands.add_parser("reconcile")
    for name in ["preview", "file"]:
        command = commands.add_parser(name)
        command.add_argument("video_id")
        command.add_argument("music_root", type=Path)
        command.add_argument("--duos", type=Path)
        command.add_argument("--duos-json")
        if name == "file":
            command.add_argument("--expected-token", required=True)
            command.add_argument("--allow-quality-replace", action="store_true")
    args = parser.parse_args()
    if args.command == "capabilities":
        print(
            json.dumps(
                {
                    "protocol": 1,
                    "in_place": True,
                    "exact_path_quality_archive": True,
                    "tagger_byte_identity": True,
                    "manual_sources": True,
                }
            )
        )
        return
    settings = get_settings()
    if not settings.intake_only:
        parser.error("Set YUBAL_INTAKE_ONLY=true")
    require_local_sqlite(settings.db_path)
    run_migrations()
    engine = create_db_engine(settings.db_path)
    try:
        ledger = IntakeLedger(engine)
        if args.command == "work":
            result = {"protocol": 1, "items": ledger.tagger_work(args.source)}
        elif args.command == "register-manual":
            if (
                not settings.intake_staging
                or args.source.resolve() != settings.intake_staging.resolve()
            ):
                raise ValueError(
                    "Manual registration is restricted to configured intake"
                )
            paths = json.loads(args.paths_json)
            if (
                not isinstance(paths, list)
                or not paths
                or len(paths) > 1000
                or not all(isinstance(p, str) for p in paths)
                or args.settle < 300
            ):
                raise ValueError(
                    "Explicit settled arrival paths required (maximum 1000)"
                )
            result = {
                "protocol": 1,
                "items": register_manual_paths(
                    ledger,
                    args.source,
                    [Path(p) for p in paths],
                    settle_seconds=args.settle,
                ),
            }
        elif args.command == "verify":
            path, pcm = ledger.tagging_candidate(args.video_id)
            audio = verify_audio(path)
            if audio.pcm_sha256 != pcm:
                raise ValueError("Source no longer matches the verified download")
            result = {
                "status": "verified",
                "path": str(path),
                "audio_sha256": audio.sha256,
            }
        elif args.command == "observe":
            status = observe_manual_tags(ledger, args.video_id)
            path, pcm = ledger.tagging_candidate(args.video_id)
            audio = verify_audio(path)
            if audio.pcm_sha256 != pcm:
                raise ValueError("Source changed after observation")
            result = {"status": status, "path": str(path), "audio_sha256": audio.sha256}
        elif args.command == "reconcile":
            result = {"reconciled": reconcile_filing(ledger)}
        else:
            duos = (
                json.loads(args.duos_json)
                if args.duos_json
                else json.loads(args.duos.read_text())
                if args.duos
                else DEFAULT_DUOS
            )
            if not isinstance(duos, (list, tuple)) or not all(
                isinstance(v, str) for v in duos
            ):
                raise ValueError("Duo configuration must be a list of canonical names")
            if args.command == "preview":
                result = preview_reviewed(ledger, args.video_id, args.music_root, duos)
            else:
                result = file_reviewed(
                    ledger,
                    args.video_id,
                    args.music_root,
                    args.expected_token,
                    allow_replace=args.allow_quality_replace,
                    duos=duos,
                )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
