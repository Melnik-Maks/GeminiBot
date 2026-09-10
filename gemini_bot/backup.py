"""Consistent SQLite backup, including committed WAL data."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


def backup(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if not source.is_file():
        raise ValueError("Database does not exist.")
    if source == target or target.exists():
        raise ValueError("Backup target must be a new file, different from the database.")
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup integrity check failed.")
    finally:
        src.close()
        dst.close()
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="data/bot.sqlite3")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    parser.add_argument("--output", default=f"backups/bot-{stamp}.sqlite3")
    args = parser.parse_args()
    print(backup(args.database, args.output))


if __name__ == "__main__":
    main()
