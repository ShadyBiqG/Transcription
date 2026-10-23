"""Check that the installed web interface is identical to the source files."""

import argparse
import sys
from pathlib import Path

import local_voice_transcriber

INTERFACE_FILES = ("index.html", "app.js", "styles.css")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the installed Transcription Next interface assets."
    )
    parser.add_argument(
        "--application-root",
        required=True,
        help="Absolute path to the local-voice-transcriber application root.",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    source_root = (
        Path(arguments.application_root).resolve()
        / "src"
        / "local_voice_transcriber"
        / "static"
    )
    installed_root = Path(local_voice_transcriber.__file__).resolve().parent / "static"
    mismatches: list[str] = []

    for name in INTERFACE_FILES:
        source_file = source_root / name
        installed_file = installed_root / name
        if not source_file.is_file():
            mismatches.append(f"missing source file: {name}")
        elif not installed_file.is_file():
            mismatches.append(f"missing installed file: {name}")
        elif source_file.read_bytes() != installed_file.read_bytes():
            mismatches.append(f"different file: {name}")

    if mismatches:
        print("Installed interface verification failed: " + "; ".join(mismatches), file=sys.stderr)
        return 1

    print("Installed interface files verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
