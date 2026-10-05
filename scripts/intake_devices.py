"""Local owner provisioning for fresh intake until interactive pairing exists."""

import argparse

from yubal_api.api.app import run_migrations
from yubal_api.db.engine import create_db_engine
from yubal_api.db.intake_ledger import IntakeLedger
from yubal_api.db.local_disk import require_local_sqlite
from yubal_api.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("provision", "revoke"))
    parser.add_argument("device_id", nargs="?", help="Required for revoke")
    args = parser.parse_args()
    settings = get_settings()
    if not settings.intake_only:
        parser.error("Set YUBAL_INTAKE_ONLY=true before provisioning devices")
    require_local_sqlite(settings.db_path)
    if args.action == "revoke" and not args.device_id:
        parser.error("revoke requires a device_id")
    run_migrations()
    engine = create_db_engine(settings.db_path)
    try:
        ledger = IntakeLedger(engine)
        if args.action == "provision":
            device_id, token = ledger.provision_device()
            # Explicit owner invocation; token is shown once and never stored raw.
            print(f"device_id={device_id}\ndevice_token={token}")
        else:
            if not ledger.revoke_device(args.device_id):
                parser.error("No active device with that ID")
            print("Device revoked")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
