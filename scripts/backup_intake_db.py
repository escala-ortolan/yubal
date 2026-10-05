"""Copy an intake DB to a NEW local file; restore by copying to another NEW file."""

import argparse
from pathlib import Path

from yubal_api.services.intake_backup import backup_intake_db
from yubal_api.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    backup_intake_db(get_settings().db_path, args.destination)
    print(f"Verified backup: {args.destination}")


if __name__ == "__main__":
    main()
