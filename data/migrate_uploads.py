"""One-off migration: archive legacy flat uploads into data/uploads/_legacy/.

2026-08-05: uploads moved to per-conversation folders (data/uploads/<conv_id>/).
Old files (uploaded before this change) have no conversation attribution in
the database (attachments were stored as base64/mount refs), so they cannot
be moved into conversation folders. This script moves every legacy flat file
into data/uploads/_legacy/ so the uploads root only contains conversation
folders. Display code still finds _legacy files via stem matching, so old
attachments keep working.

Usage:  python data/migrate_uploads.py   (idempotent — safe to re-run)
"""
from __future__ import annotations

import shutil
from pathlib import Path

UPLOADS = Path(__file__).parent / "uploads"
LEGACY = UPLOADS / "_legacy"


def is_hex16(name: str) -> bool:
    stem = Path(name).stem
    return len(stem) == 16 and all(c in "0123456789abcdef" for c in stem)


def migrate() -> None:
    if not UPLOADS.is_dir():
        print("uploads dir missing — nothing to do")
        return
    LEGACY.mkdir(parents=True, exist_ok=True)
    moved = skipped = 0
    for fp in sorted(UPLOADS.iterdir()):
        if not fp.is_file():
            continue  # already a folder (conversation dir or _legacy)
        # Only files that look like uploads (16-hex id + optional ext) move;
        # anything else (e.g. stray files) also goes to _legacy to keep the
        # root clean, unless it's already a hex-named file we've seen.
        dest = LEGACY / fp.name
        if dest.exists():
            # Same name already archived (re-run) — remove the loose copy
            fp.unlink()
            skipped += 1
            continue
        shutil.move(str(fp), str(dest))
        moved += 1
    print(f"migrated {moved} files into {LEGACY} (skipped {skipped} re-runs)")


if __name__ == "__main__":
    migrate()
