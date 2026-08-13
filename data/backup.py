"""
AI Chat - Backup & Restore Utility

Usage:
    python backup.py backup              # Run a backup now
    python backup.py backup --retention 30  # Keep last 30 backups
    python backup.py restore <zip_file>  # Restore from a backup zip
    python backup.py list                # List available backups

Can also be imported and called programmatically:
    from backup import run_backup, restore_backup, list_backups
"""

import zipfile
import shutil
import logging
from datetime import datetime
from pathlib import Path

# ============================================================
# Configuration
# ============================================================
BASE_DIR = Path(__file__).parent  # data/
BACKUP_DIR = BASE_DIR / "backups"
LOG_DIR = Path(__file__).parent.parent / "logs"

# Files and directories to back up (relative to BASE_DIR)
BACKUP_TARGETS = [
    "config.yaml",
    ".secret_key",
    "chat.db",            # SQLite database (users + conversations + messages)
    "alerts",             # alert config and history
]

# Default retention: keep last N backups (0 = unlimited)
DEFAULT_RETENTION = 30

# Logger
logger = logging.getLogger("ai-chat.backup")


def _setup_logger():
    """Configure backup logging."""
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [BACKUP] %(levelname)s %(message)s")

    # Console
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File (append to app.log)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)


# ============================================================
# Backup
# ============================================================

def run_backup(backup_dir: Path = None, retention: int = None) -> Path:
    """
    Create a timestamped backup zip.

    Returns the path to the created zip file.
    """
    _setup_logger()
    backup_dir = backup_dir or BACKUP_DIR
    retention = retention if retention is not None else DEFAULT_RETENTION

    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"ai-chat-backup-{timestamp}.zip"
    zip_path = backup_dir / zip_name

    logger.info("Starting backup -> %s", zip_path)

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for target in BACKUP_TARGETS:
                target_path = BASE_DIR / target
                if not target_path.exists():
                    logger.warning("Skipping missing target: %s", target)
                    continue

                if target_path.is_file():
                    # Single file
                    arcname = f"data/{target}"
                    zf.write(target_path, arcname)
                    logger.info("  + %s", arcname)

                elif target_path.is_dir():
                    # Directory - walk and add all files
                    for file_path in sorted(target_path.rglob("*")):
                        if file_path.is_file():
                            rel = file_path.relative_to(BASE_DIR)
                            zf.write(file_path, f"data/{rel}")
                            logger.info("  + data/%s", rel)

        size_kb = zip_path.stat().st_size / 1024
        logger.info("Backup complete: %s (%.1f KB)", zip_name, size_kb)

        # Rotate old backups
        _rotate_backups(backup_dir, retention)

        return zip_path

    except Exception:
        # Clean up partial zip on failure
        if zip_path.exists():
            zip_path.unlink()
        logger.exception("Backup failed")
        raise


def _rotate_backups(backup_dir: Path, retention: int):
    """Keep only the most recent `retention` backups."""
    if retention <= 0:
        return

    backups = sorted(
        backup_dir.glob("ai-chat-backup-*.zip"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    removed = 0
    for old_backup in backups[retention:]:
        old_backup.unlink()
        removed += 1
        logger.info("  Removed old backup: %s", old_backup.name)

    if removed:
        logger.info("Rotation: kept %d, removed %d (retention=%d)",
                     min(len(backups), retention), removed, retention)


# ============================================================
# Restore
# ============================================================

def restore_backup(zip_path: Path, base_dir: Path = None, dry_run: bool = False) -> list:
    """
    Restore data from a backup zip.

    Args:
        zip_path: Path to the backup zip
        base_dir: Target directory (defaults to data/)
        dry_run: If True, only list what would be restored

    Returns:
        List of restored file paths
    """
    _setup_logger()
    base_dir = base_dir or BASE_DIR

    if not zip_path.exists():
        raise FileNotFoundError(f"Backup not found: {zip_path}")

    logger.info("Restoring from: %s (dry_run=%s)", zip_path.name, dry_run)

    restored = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if not info.filename.startswith("data/"):
                continue

            # Strip "data/" prefix to get relative path
            rel_path = info.filename[5:]  # len("data/") = 5
            if not rel_path:
                continue

            target = base_dir / rel_path

            if dry_run:
                logger.info("  [dry-run] Would restore: %s", rel_path)
                restored.append(rel_path)
                continue

            # Ensure parent directory exists
            target.parent.mkdir(parents=True, exist_ok=True)

            if info.is_dir():
                continue

            # Extract file
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

            logger.info("  Restored: %s", rel_path)
            restored.append(rel_path)

    logger.info("Restore complete: %d files", len(restored))
    return restored


# ============================================================
# List backups
# ============================================================

def list_backups(backup_dir: Path = None) -> list:
    """Return a sorted list of backup dicts with name, size, date."""
    backup_dir = backup_dir or BACKUP_DIR
    if not backup_dir.exists():
        return []

    backups = []
    for f in sorted(backup_dir.glob("ai-chat-backup-*.zip")):
        stat = f.stat()
        backups.append({
            "name": f.name,
            "path": str(f),
            "size_kb": round(stat.st_size / 1024, 1),
            "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })

    return sorted(backups, key=lambda b: b["created"], reverse=True)


# ============================================================
# CLI
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description="AI Chat Backup Utility")
    sub = parser.add_subparsers(dest="command")

    # backup
    p_backup = sub.add_parser("backup", help="Create a backup")
    p_backup.add_argument("--retention", type=int, default=DEFAULT_RETENTION,
                          help=f"Max backups to keep (default: {DEFAULT_RETENTION})")
    p_backup.add_argument("--dir", type=str, default=None,
                          help="Backup directory (default: data/backups)")

    # restore
    p_restore = sub.add_parser("restore", help="Restore from backup")
    p_restore.add_argument("zip_file", help="Path to backup zip")
    p_restore.add_argument("--dry-run", action="store_true",
                           help="Show what would be restored without writing")
    p_restore.add_argument("--target", type=str, default=None,
                           help="Target directory (default: data/)")

    # list
    sub.add_parser("list", help="List available backups")

    args = parser.parse_args()

    if args.command == "backup":
        bdir = Path(args.dir) if args.dir else None
        path = run_backup(backup_dir=bdir, retention=args.retention)
        print(f"\nBackup created: {path}")

    elif args.command == "restore":
        target = Path(args.target) if args.target else None
        files = restore_backup(
            Path(args.zip_file),
            base_dir=target,
            dry_run=args.dry_run,
        )
        print(f"\nRestored {len(files)} files")

    elif args.command == "list":
        backups = list_backups()
        if not backups:
            print("No backups found.")
        else:
            print(f"\n{'Name':<40} {'Size':>8}  {'Created'}")
            print("-" * 75)
            for b in backups:
                print(f"{b['name']:<40} {b['size_kb']:>6.1f}KB  {b['created']}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
