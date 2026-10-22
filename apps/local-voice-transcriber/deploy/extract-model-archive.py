"""Безопасно извлекает проверенный tar-архив model pack во временный каталог."""

from __future__ import annotations

import argparse
import tarfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    arguments = parser.parse_args()
    destination = arguments.destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)

    with tarfile.open(arguments.archive, mode="r:bz2") as archive:
        members = archive.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination)
            except ValueError as error:
                raise ValueError("Архив содержит путь вне временного каталога.") from error
            if not (member.isdir() or member.isfile()):
                raise ValueError("Архив model pack содержит недопустимый тип файла.")
        archive.extractall(destination, members=members, filter="data")


if __name__ == "__main__":
    main()
