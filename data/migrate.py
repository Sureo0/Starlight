"""
AI Chat - JSON to SQLite Migration Script

Migrates existing JSON-based data (users.json, conversations/*.json)
into the SQLite database (chat.db).

Usage:
    python migrate.py              # Run migration
    python migrate.py --dry-run    # Preview without writing
    python migrate.py --backup     # Create backup before migrating

The script is idempotent - safe to run multiple times.
"""

import json
import sys
import shutil
import logging
from datetime import datetime, timezone
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from database import db, DB_FILE

BASE_DIR = Path(__file__).parent.parent  # project root
DATA_DIR = BASE_DIR / "data"
USERS_FILE = DATA_DIR / "users.json"
CONVERSATIONS_DIR = DATA_DIR / "conversations"

logger = logging.getLogger("migrate")


def backup_json_files():
    """Create a backup of JSON files before migration."""
    backup_dir = DATA_DIR / "json_backup"
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_subdir = backup_dir / f"pre_migration_{timestamp}"
    backup_subdir.mkdir(parents=True, exist_ok=True)

    # Copy users.json
    if USERS_FILE.exists():
        shutil.copy2(USERS_FILE, backup_subdir / "users.json")
        print(f"  Backed up: users.json")

    # Copy conversations
    if CONVERSATIONS_DIR.exists():
        conv_backup = backup_subdir / "conversations"
        conv_backup.mkdir(exist_ok=True)
        for f in CONVERSATIONS_DIR.glob("*.json"):
            shutil.copy2(f, conv_backup / f.name)
        count = len(list(CONVERSATIONS_DIR.glob("*.json")))
        print(f"  Backed up: {count} conversation(s)")

    print(f"  Backup location: {backup_subdir}")
    return backup_subdir


def migrate_users(dry_run=False):
    """Migrate users from users.json to SQLite."""
    if not USERS_FILE.exists():
        print("  No users.json found, skipping.")
        return 0

    users = json.loads(USERS_FILE.read_text("utf-8"))
    count = 0

    for username, data in users.items():
        # Check if user already exists
        existing = db.get_user(username)
        if existing:
            print(f"  [skip] User '{username}' already exists in DB")
            continue

        if dry_run:
            print(f"  [dry-run] Would create user: {username}")
            count += 1
            continue

        password_hash = data.get("password", "")
        created_at = data.get("created_at", datetime.now(timezone.utc).isoformat())

        conn = db._get_conn()
        conn.execute(
            "INSERT INTO users (username, password, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, created_at, data.get("updated_at")),
        )
        conn.commit()
        print(f"  [ok] User: {username}")
        count += 1

    return count


def migrate_conversations(dry_run=False):
    """Migrate conversations from JSON files to SQLite."""
    if not CONVERSATIONS_DIR.exists():
        print("  No conversations directory found, skipping.")
        return 0

    files = list(CONVERSATIONS_DIR.glob("*.json"))
    count = 0

    for f in files:
        conv_id = f.stem
        try:
            data = json.loads(f.read_text("utf-8"))
        except Exception as e:
            print(f"  [error] Failed to read {f.name}: {e}")
            continue

        # Check if conversation already exists
        existing = db.get_conversation(conv_id)
        if existing:
            print(f"  [skip] Conversation {conv_id} already exists in DB")
            continue

        if dry_run:
            msg_count = len(data.get("messages", []))
            print(f"  [dry-run] Would create conversation: {conv_id} ({msg_count} messages)")
            count += 1
            continue

        # Create conversation
        title = data.get("title", "New Chat")
        created_at = data.get("created_at", datetime.now(timezone.utc).isoformat())
        updated_at = data.get("updated_at", created_at)

        conn = db._get_conn()
        try:
            conn.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (conv_id, title, created_at, updated_at),
            )

            # Migrate messages
            messages = data.get("messages", [])
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                ts = msg.get("timestamp", datetime.now(timezone.utc).isoformat())
                if role in ("user", "assistant", "system") and content:
                    conn.execute(
                        "INSERT INTO messages (conversation_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                        (conv_id, role, content, ts),
                    )

            conn.commit()
            print(f"  [ok] Conversation: {conv_id} ({len(messages)} messages)")
            count += 1

        except Exception as e:
            conn.rollback()
            print(f"  [error] Failed to migrate {conv_id}: {e}")

    return count


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Migrate JSON data to SQLite")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--backup", action="store_true", help="Backup JSON files first")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("\n" + "=" * 50)
    print("  AI Chat - JSON to SQLite Migration")
    print("=" * 50)

    if args.dry_run:
        print("\n  MODE: Dry run (no changes will be made)\n")
    else:
        print(f"\n  Database: {DB_FILE}\n")

    # Backup
    if args.backup and not args.dry_run:
        print("[1] Backing up JSON files...")
        backup_json_files()
        print()

    # Migrate users
    print("[2] Migrating users...")
    user_count = migrate_users(dry_run=args.dry_run)
    print(f"    -> {user_count} user(s) processed\n")

    # Migrate conversations
    print("[3] Migrating conversations...")
    conv_count = migrate_conversations(dry_run=args.dry_run)
    print(f"    -> {conv_count} conversation(s) processed\n")

    # Summary
    if not args.dry_run:
        stats = db.get_stats()
        print("[4] Database statistics:")
        print(f"    Users:         {stats['users']}")
        print(f"    Conversations: {stats['conversations']}")
        print(f"    Messages:      {stats['messages']}")
        print(f"    DB size:       {stats['db_size_kb']} KB")
        print()

    print("=" * 50)
    print("  Migration complete!")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
