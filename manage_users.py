"""
User management script for AI Chat.
Storage: SQLite (data/chat.db) — the SAME store the app's login reads from.

NOTE: The old version of this script wrote to data/users.json, which the
login system does NOT read. If you have users in users.json that are not
yet in SQLite, run `python data/migrate.py` once to import them.

Usage:
    python manage_users.py add <username> <password>
    python manage_users.py delete <username>
    python manage_users.py list
"""
import sys
import json
from pathlib import Path
from werkzeug.security import generate_password_hash

# Resolve project root and import the SQLite-backed db (same as auth.py)
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "data"))

from data.database import db, DB_FILE  # noqa: E402

USERS_FILE = Path(__file__).parent / "data" / "users.json"


def _ensure_legacy_migrated():
    """Warn if users.json has users that aren't in SQLite yet (they would
    be invisible to login). Returns True if everything is consistent."""
    if not USERS_FILE.exists():
        return True
    try:
        legacy = json.loads(USERS_FILE.read_text("utf-8"))
    except Exception:
        return True
    missing = [name for name in legacy if db.get_user(name) is None]
    if missing:
        print(
            f"⚠  Warning: {len(missing)} user(s) exist only in legacy users.json "
            f"(not in SQLite): {', '.join(missing)}"
        )
        print("   Run `python data/migrate.py` to import them, or add them here.")
        return False
    return True


def cmd_add(username, password):
    """Create a user in SQLite (the store login actually reads)."""
    # Same validation as auth.create_user
    if not username or not all(c.isalnum() or c == "_" for c in username):
        print(f"Invalid username '{username}': only letters, digits, underscore; 3-20 chars.")
        return
    if len(username) < 3 or len(username) > 20:
        print(f"Invalid username '{username}': must be 3-20 characters.")
        return
    if db.get_user(username):
        print(f"User '{username}' already exists (SQLite).")
        return
    password_hash = generate_password_hash(password)
    user_id = db.create_user(username, password_hash)
    if user_id is not None:
        print(f"User '{username}' created (id={user_id}, SQLite: {DB_FILE}).")
    else:
        print(f"Failed to create user '{username}'.")


def cmd_delete(username):
    """Delete a user from SQLite."""
    if db.delete_user(username):
        print(f"User '{username}' deleted from SQLite.")
    else:
        print(f"User '{username}' not found.")


def cmd_list():
    """List users from SQLite."""
    users = db.list_users()
    if not users:
        print("No users found (SQLite).")
        return
    print(f"Users in SQLite ({len(users)}):")
    for name in users:
        print(f"  - {name}")
    _ensure_legacy_migrated()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    _ensure_legacy_migrated()

    cmd = sys.argv[1]
    if cmd == "add" and len(sys.argv) == 4:
        cmd_add(sys.argv[2], sys.argv[3])
    elif cmd == "delete" and len(sys.argv) == 3:
        cmd_delete(sys.argv[2])
    elif cmd == "list":
        cmd_list()
    else:
        print(__doc__)
        sys.exit(1)
