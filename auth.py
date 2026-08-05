"""
Authentication module for AI Chat.
Uses Flask sessions + werkzeug password hashing + SQLite storage.
"""
import sys
import functools
import logging
import time
import threading
from datetime import datetime
from pathlib import Path

# Add data/ to path for database module
sys.path.insert(0, str(Path(__file__).parent / "data"))

from flask import (
    Blueprint, request, session, redirect, url_for, render_template, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash

from database import db

logger = logging.getLogger("ai-chat")

auth_bp = Blueprint("auth", __name__)


# ============================================================
# Rate limiter (in-memory, per IP)
# ============================================================
class RateLimiter:
    """Simple in-memory rate limiter for login attempts."""

    def __init__(self, max_attempts=5, window=300, lockout=300):
        self._lock = threading.Lock()
        self._attempts = {}  # {ip: [(timestamp, success), ...]}
        self.max_attempts = max_attempts
        self.window = window
        self.lockout = lockout
        # Periodic cleanup thread
        self._cleanup_thread = threading.Thread(
            target=self._periodic_cleanup, daemon=True
        )
        self._cleanup_thread.start()

    def is_locked(self, ip):
        """Check if an IP is currently locked out."""
        with self._lock:
            self._cleanup_old()
            if ip not in self._attempts:
                return False
            attempts = self._attempts[ip]
            if not attempts:
                return False
            failures = [t for t, s in attempts if not s]
            if len(failures) >= self.max_attempts:
                last_failure = max(t for t, s in attempts if not s)
                if time.time() - last_failure < self.lockout:
                    return True
            return False

    def record(self, ip, success):
        """Record a login attempt."""
        with self._lock:
            if ip not in self._attempts:
                self._attempts[ip] = []
            self._attempts[ip].append((time.time(), success))
            self._cleanup_old()

    def _cleanup_old(self):
        """Remove all entries outside the window (must hold lock)."""
        cutoff = time.time() - self.window
        empty_ips = []
        for ip in self._attempts:
            self._attempts[ip] = [
                (t, s) for t, s in self._attempts[ip] if t > cutoff
            ]
            if not self._attempts[ip]:
                empty_ips.append(ip)
        for ip in empty_ips:
            del self._attempts[ip]

    def _periodic_cleanup(self):
        """Background thread to periodically clean up stale entries."""
        while True:
            time.sleep(60)
            with self._lock:
                self._cleanup_old()


rate_limiter = RateLimiter(max_attempts=5, window=300, lockout=300)


# ============================================================
# User storage (SQLite)
# ============================================================

def create_user(username, password):
    """Create a new user. Returns True on success, False if user exists."""
    # Validate username format
    if not username or not all(c.isalnum() or c == "_" for c in username):
        return False
    if len(username) < 3 or len(username) > 20:
        return False
    password_hash = generate_password_hash(password)
    result = db.create_user(username, password_hash)
    return result is not None


def delete_user(username):
    """Delete a user. Returns True if deleted."""
    return db.delete_user(username)


def authenticate(username, password):
    """Verify credentials. Returns True if valid."""
    # Validate username format (alphanumeric + underscore, 3-20 chars)
    if not username or not all(c.isalnum() or c == "_" for c in username):
        return False
    if len(username) < 3 or len(username) > 20:
        return False
    user = db.get_user(username)
    if user and check_password_hash(user["password"], password):
        return True
    return False


def list_users():
    """Return list of usernames."""
    return db.list_users()


def change_password(username, current_password, new_password):
    """Change a user's password. Returns (True, None) or (False, error_msg)."""
    user = db.get_user(username)
    if not user:
        return False, "User not found"
    if not check_password_hash(user["password"], current_password):
        return False, "Current password is incorrect"
    # Password strength validation
    if len(new_password) < 8:
        return False, "Password must be at least 8 characters"
    if not any(c.isupper() for c in new_password):
        return False, "Password must contain at least one uppercase letter"
    if not any(c.islower() for c in new_password):
        return False, "Password must contain at least one lowercase letter"
    if not any(c.isdigit() for c in new_password):
        return False, "Password must contain at least one number"
    new_hash = generate_password_hash(new_password)
    db.update_user(username, password_hash=new_hash)
    return True, None


# ============================================================
# Decorator
# ============================================================

def login_required(f):
    """Redirect to /login if not authenticated."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            session["next"] = request.url
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


# ============================================================
# Routes
# ============================================================

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "unknown"

        # Check rate limit
        if rate_limiter.is_locked(ip):
            logger.warning("Login blocked (rate limit): ip=%s", ip)
            error = "Too many failed attempts. Please try again later."
            return render_template("login.html", error=error), 429

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if authenticate(username, password):
            rate_limiter.record(ip, True)
            # Clear old session data to prevent session fixation
            session.clear()
            session["user"] = username
            session.permanent = True
            logger.info("Login success: user=%s ip=%s", username, ip)
            next_url = session.pop("next", None) or "/"
            return redirect(next_url)
        rate_limiter.record(ip, False)
        logger.warning("Login failed: user=%s ip=%s", username, ip)
        error = "Username or password is incorrect"
    return render_template("login.html", error=error)


@auth_bp.route("/logout")
def logout():
    user = session.get("user", "unknown")
    logger.info("Logout: user=%s ip=%s", user, request.remote_addr)
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.route("/api/user")
def api_user_info():
    """Return current logged-in user (for frontend)."""
    user = session.get("user")
    if user:
        return jsonify({"user": user})
    return jsonify({"user": None}), 401


@auth_bp.route("/api/change-password", methods=["POST"])
def api_change_password():
    """Change the current user's password."""
    user = session.get("user")
    if not user:
        return jsonify({"error": "Not logged in"}), 401

    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")

    if not current_password or not new_password:
        return jsonify({"error": "Both current and new password are required"}), 400

    if len(new_password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    ok, err = change_password(user, current_password, new_password)
    if not ok:
        logger.warning("Password change failed for user %s: %s", user, err)
        return jsonify({"error": err}), 400

    logger.info("Password changed for user %s", user)
    return jsonify({"ok": True})
