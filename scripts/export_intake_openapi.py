"""Export the generated fresh-mode OpenAPI for backend/client fixture checks."""

import argparse
import json
from pathlib import Path

from yubal_api.api.app import create_app
from yubal_api.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if not get_settings().intake_only:
        parser.error("Set YUBAL_INTAKE_ONLY=true before exporting the client schema")
    schema = create_app().openapi()
    if any(path.startswith("/api/") for path in schema["paths"]):
        raise RuntimeError("Legacy routes appeared in the fresh-mode schema")
    args.output.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
