#!/usr/bin/env python3
"""
VelosifyCredit backup.

Everything a client gives us lives in two places: rows in SQLite, and files
in the upload directory. A backup of one without the other is useless, so
this takes both, together, into one timestamped archive.

    python backup.py                     # write into ./backups
    python backup.py --out /mnt/backups  # somewhere else
    python backup.py --keep 30           # prune archives older than this many
    python backup.py --verify FILE       # check an archive is readable

The database is copied with SQLite's own online backup API rather than by
copying the file. Copying a live SQLite file, or its WAL, can capture a torn
transaction and produce an archive that only fails when you try to restore
it, which is the worst possible time to find out.

Run it from cron, from a Railway scheduled job, or by hand before a risky
deploy. It never touches the live data.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def snapshot_database(dest: Path) -> int:
    """Consistent copy of the database, taken while it is in use.

    sqlite3's backup API walks the pages under a read lock and retries any it
    sees change underneath it, so the result is a valid database at a single
    point in time, WAL included.
    """
    src = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    out = sqlite3.connect(str(dest))
    try:
        src.backup(out)
        out.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # A backup you have not opened is a hope, not a backup.
        count = out.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        integrity = out.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"integrity_check said: {integrity}")
        return int(count)
    finally:
        src.close()
        out.close()


def create(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"velosify-backup-{_stamp()}.tar.gz"

    with tempfile.TemporaryDirectory() as work:
        db_copy = Path(work) / "velosify.db"
        users = snapshot_database(db_copy)

        with tarfile.open(archive, "w:gz") as tar:
            tar.add(db_copy, arcname="velosify.db")
            if config.UPLOAD_DIR.exists():
                tar.add(config.UPLOAD_DIR, arcname="uploads")
            manifest = Path(work) / "MANIFEST.txt"
            manifest.write_text(
                f"created_at: {datetime.now(timezone.utc).isoformat()}\n"
                f"users: {users}\n"
                f"db_source: {config.DB_PATH}\n"
                f"uploads_source: {config.UPLOAD_DIR}\n"
                "\n"
                "To restore: stop the app, extract this archive, put\n"
                "velosify.db and uploads/ back at the paths above, start it.\n"
            )
            tar.add(manifest, arcname="MANIFEST.txt")

    size_mb = archive.stat().st_size / (1024 * 1024)
    print(f"wrote {archive} ({size_mb:.1f} MB, {users} users)")
    return archive


def verify(archive: Path) -> bool:
    """Extract to a scratch directory and open the database inside it."""
    with tempfile.TemporaryDirectory() as work:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(work, filter="data")
        db = Path(work) / "velosify.db"
        if not db.exists():
            print(f"{archive}: no database inside", file=sys.stderr)
            return False
        conn = sqlite3.connect(str(db))
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                print(f"{archive}: integrity check failed", file=sys.stderr)
                return False
            users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        finally:
            conn.close()
        files = len(list((Path(work) / "uploads").glob("*"))) if (Path(work) / "uploads").exists() else 0
    print(f"{archive.name}: ok — {users} users, {docs} document rows, {files} files")
    return True


def prune(out_dir: Path, keep: int) -> None:
    archives = sorted(out_dir.glob("velosify-backup-*.tar.gz"))
    for old in archives[:-keep] if keep > 0 else []:
        old.unlink()
        print(f"pruned {old.name}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.environ.get("BACKUP_DIR", "backups"),
                    help="directory to write archives into (default: ./backups)")
    ap.add_argument("--keep", type=int, default=14,
                    help="how many archives to retain (default: 14, 0 = all)")
    ap.add_argument("--verify", metavar="ARCHIVE",
                    help="verify an existing archive instead of making one")
    args = ap.parse_args()

    if args.verify:
        return 0 if verify(Path(args.verify)) else 1

    out_dir = Path(args.out)
    archive = create(out_dir)
    if not verify(archive):
        return 1
    prune(out_dir, args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
