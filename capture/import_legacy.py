"""Copy the Lecture Today backlog into the Flashcap library.

Usage::

    python -m capture.import_legacy
    python -m capture.import_legacy --legacy /path/to/lecture_today.db --db /tmp/cards.db

The old database is opened read-only. Running this twice adds nothing.
"""

from __future__ import annotations

import argparse

from .config import PROJECT_ROOT, get_db_path
from .library import import_legacy


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Copy the Lecture Today backlog into the Flashcap library."
    )
    parser.add_argument(
        "--legacy",
        default=str(PROJECT_ROOT / "lecture_today.db"),
        help="Path to lecture_today.db (read-only).",
    )
    parser.add_argument(
        "--db",
        default=get_db_path(),
        help="Path to the Flashcap database.",
    )
    args = parser.parse_args(argv)

    from .server import create_app

    app = create_app(args.db)
    with app.app_context():
        counts = import_legacy(args.legacy)

    print(
        f"Read {counts['seen']} rows, added {counts['added']}, "
        f"library now holds {counts['total']}."
    )


if __name__ == "__main__":
    main()
