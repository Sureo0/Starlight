"""
AI Chat - Monitoring & Alerting Engine

Provides:
- System resource monitoring (CPU, memory, disk)
- LLM backend health checks
- Configurable alert rules with thresholds
- Notification channels: webhook, email, in-app
- Alert history with persistence

Usage (standalone):
    python alerts.py check          # Run all health checks now
    python alerts.py history        # Show recent alerts
    python alerts.py test-webhook   # Send a test webhook
"""

import json
import logging
import smtplib
import ssl
import threading
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

import requests

# ============================================================
# Configuration
# ============================================================
BASE_DIR = Path(__file__).parent  # data/
ALERTS_DIR = BASE_DIR / "alerts"
ALERTS_DIR.mkdir(parents=True, exist_ok=True)

ALERT_HISTORY_FILE = ALERTS_DIR / "history.json"
ALERT_CONFIG_FILE = ALERTS_DIR / "config.json"

logger = logging.getLogger("ai-chat.alerts")

# ============================================================
# Default alert configuration
# ============================================================
DEFAULT_ALERT_CONFIG = {
    "enabled": True,
    "check_interval": 60,  # seconds between health checks

    # Notification channels
    "notifications": {
        "in_app": True,
        "webhook": {
            "enabled": False,
            "url": "",          # DingTalk / WeCom / Slack webhook URL
            "type": "dingtalk",  # dingtalk | wecom | slack | generic
            "secret": "",       # Optional signing secret (DingTalk)
        },
        "email": {
            "enabled": False,
            "smtp_host": "",
            "smtp_port": 465,
            "smtp_user": "",
            "smtp_pass": "",
            "from_addr": "",
            "to_addrs": [],     # List of recipient emails
            "use_tls": True,
        },
    },

    # Alert rules
    "rules": {
        "cpu_high": {
            "enabled": True,
            "threshold": 90,        # percent
            "duration": 120,        # sustained for N seconds
            "severity": "warning",
            "message": "CPU usage above {threshold}% for {duration}s (current: {current}%)",
        },
        "memory_high": {
            "enabled": True,
            "threshold": 85,
            "duration": 120,
            "severity": "warning",
            "message": "Memory usage above {threshold}% for {duration}s (current: {current}%)",
        },
        "disk_low": {
            "enabled": True,
            "threshold": 10,        # free space below N%
            "severity": "critical",
            "message": "Disk free space below {threshold}% (current: {current}% free)",
        },
        "llm_error_rate": {
            "enabled": True,
            "threshold": 30,        # error rate percent
            "min_calls": 5,         # minimum calls before evaluating
            "severity": "warning",
            "message": "LLM error rate above {threshold}% ({current}%)",
        },
        "llm_latency": {
            "enabled": True,
            "threshold": 30000,     # ms
            "min_calls": 3,
            "severity": "warning",
            "message": "LLM avg latency above {threshold}ms (current: {current}ms)",
        },
        "llm_unreachable": {
            "enabled": True,
            "severity": "critical",
            "message": "LLM backend unreachable: {error}",
        },
        "login_failures": {
            "enabled": True,
            "threshold": 10,        # failures in check window
            "severity": "warning",
            "message": "Multiple login failures detected: {current} recent failures",
        },
    },
}


# ============================================================
# Alert history
# ============================================================
class AlertHistory:
    """Persistent alert history with file backing."""

    def __init__(self, max_entries=500):
        self._lock = threading.Lock()
        self._max = max_entries
        self._entries = self._load()

    def _load(self):
        if ALERT_HISTORY_FILE.exists():
            try:
                return json.loads(ALERT_HISTORY_FILE.read_text("utf-8"))
            except Exception:
                return []
        return []

    def _save(self):
        try:
            ALERT_HISTORY_FILE.write_text(
                json.dumps(self._entries[-self._max:], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("Failed to save alert history")

    def add(self, rule_name, severity, message):
        with self._lock:
            entry = {
                "rule": rule_name,
                "severity": severity,
                "message": message,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            # Deduplicate: skip if same rule fired within last 60s
            if self._entries:
                last = self._entries[-1]
                if last["rule"] == rule_name:
                    try:
                        last_time = datetime.fromisoformat(last["timestamp"])
                        now = datetime.now(timezone.utc)
                        if (now - last_time).total_seconds() < 60:
                            return  # Skip duplicate
                    except Exception:
                        pass
            self._entries.append(entry)
            self._save()
            return entry

    def get_recent(self, limit=50):
        with self._lock:
            return list(reversed(self._entries[-limit:]))


# ============================================================
# System resource monitor
# ============================================================
class SystemMonitor:
    """Collect system resource metrics."""

    def __init__(self):
        self._cpu_samples = []
        self._cpu_lock = threading.Lock()

    def get_resources(self):
        """Return current system resource usage."""
        if not HAS_PSUTIL:
            return {
                "cpu_percent": 0, "cpu_avg_1m": 0,
                "memory_total_mb": 0, "memory_used_mb": 0, "memory_percent": 0,
                "disk_total_gb": 0, "disk_used_gb": 0, "disk_free_gb": 0, "disk_percent": 0,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "note": "psutil not installed - install it for system monitoring",
            }
        try:
            cpu_percent = psutil.cpu_percent(interval=0.5)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")

            # CPU average over recent samples
            with self._cpu_lock:
                self._cpu_samples.append(cpu_percent)
                if len(self._cpu_samples) > 30:
                    self._cpu_samples.pop(0)
                cpu_avg = sum(self._cpu_samples) / len(self._cpu_samples)

            return {
                "cpu_percent": cpu_percent,
                "cpu_avg_1m": round(cpu_avg, 1),
                "memory_total_mb": round(mem.total / (1024 * 1024)),
                "memory_used_mb": round(mem.used / (1024 * 1024)),
                "memory_percent": mem.percent,
                "disk_total_gb": round(disk.total / (1024 ** 3), 1),
                "disk_used_gb": round(disk.used / (1024 ** 3), 1),
                "disk_free_gb": round(disk.free / (1024 ** 3), 1),
                "disk_percent": disk.percent,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            logger.exception("Failed to collect system resources")
            return {"error": str(e)}

    def check_disk_space(self, threshold=10):
        """Check if disk free space is below threshold."""
        if not HAS_PSUTIL:
            return False, -1
        try:
            disk = psutil.disk_usage("/")
            free_percent = 100 - disk.percent
            return free_percent < threshold, free_percent
        except Exception:
            return False, -1


# ============================================================
# LLM health checker
# ============================================================
class LLMHealthChecker:
    """Check LLM backend availability."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "AI-Chat-Monitor/1.0"})

    def check_backend(self, backend):
        """Ping an LLM backend. Returns (ok, error_message)."""
        api_base = backend.get("api_base", "").rstrip("/")
        api_key = backend.get("api_key", "")
        if not api_base:
            return False, "No API base URL configured"

        try:
            resp = self._session.get(
                f"{api_base}/models",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                timeout=10,
            )
            if resp.status_code == 200:
                return True, None
            elif resp.status_code == 401:
                return False, f"Authentication failed (401)"
            else:
                return False, f"Backend returned {resp.status_code}"
        except requests.exceptions.Timeout:
            return False, "Connection timed out"
        except requests.exceptions.ConnectionError as e:
            return False, f"Connection failed: {e}"
        except Exception as e:
            return False, f"Unexpected error: {e}"


# ============================================================
# Notification channels
# ============================================================

def send_webhook(config, title, message, severity="info"):
    """Send alert via webhook (DingTalk / WeCom / Slack / generic)."""
    if not config.get("enabled") or not config.get("url"):
        return

    wh_type = config.get("type", "generic")
    url = config["url"]
    secret = config.get("secret", "")

    severity_emoji = {
        "info": "ℹ️",
        "warning": "⚠️",
        "critical": "🔴",
    }
    emoji = severity_emoji.get(severity, "📢")
    full_msg = f"{emoji} **{title}**\n\n{message}"

    try:
        if wh_type == "dingtalk":
            payload = {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": full_msg},
            }
        elif wh_type == "wecom":
            payload = {
                "msgtype": "markdown",
                "markdown": {"content": full_msg},
            }
        elif wh_type == "slack":
            payload = {"text": full_msg}
        else:
            payload = {"text": full_msg, "title": title, "severity": severity}

        # DingTalk signing
        if wh_type == "dingtalk" and secret:
            import hashlib
            import hmac
            import base64
            import urllib.parse
            timestamp = str(round(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{secret}"
            hmac_code = hmac.new(
                secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            url = f"{url}&timestamp={timestamp}&sign={sign}"

        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("Webhook sent: %s", title)
        else:
            logger.warning("Webhook failed (%d): %s", resp.status_code, resp.text[:200])
    except Exception:
        logger.exception("Webhook error")


def send_email(config, subject, body, severity="info"):
    """Send alert via email."""
    if not config.get("enabled") or not config.get("smtp_host"):
        return

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"[AI Chat {severity.upper()}] {subject}"
        msg["From"] = config.get("from_addr", config.get("smtp_user", ""))
        msg["To"] = ", ".join(config.get("to_addrs", []))

        context = ssl.create_default_context()
        if config.get("use_tls", True):
            server = smtplib.SMTP_SSL(config["smtp_host"], config.get("smtp_port", 465), context=context)
        else:
            server = smtplib.SMTP(config["smtp_host"], config.get("smtp_port", 587))
            server.starttls(context=context)

        server.login(config["smtp_user"], config["smtp_pass"])
        server.send_message(msg)
        server.quit()
        logger.info("Email sent: %s", subject)
    except Exception:
        logger.exception("Email error")


# ============================================================
# Alert Engine
# ============================================================
class AlertEngine:
    """Main alerting engine - coordinates monitoring, rules, and notifications."""

    def __init__(self, app_metrics=None):
        self.history = AlertHistory()
        self.system = SystemMonitor()
        self.llm_checker = LLMHealthChecker()
        self.metrics = app_metrics  # Reference to app's Metrics instance
        self._config = self._load_config()
        self._lock = threading.Lock()
        self._timer = None
        self._running = False

        # Track sustained conditions for duration-based rules
        self._sustained = {}  # {rule_name: first_triggered_timestamp}

    def _load_config(self):
        if ALERT_CONFIG_FILE.exists():
            try:
                user_cfg = json.loads(ALERT_CONFIG_FILE.read_text("utf-8"))
                # Merge with defaults
                cfg = DEFAULT_ALERT_CONFIG.copy()
                cfg.update(user_cfg)
                cfg["rules"] = {**DEFAULT_ALERT_CONFIG["rules"], **user_cfg.get("rules", {})}
                cfg["notifications"] = {**DEFAULT_ALERT_CONFIG["notifications"], **user_cfg.get("notifications", {})}
                return cfg
            except Exception:
                logger.exception("Failed to load alert config, using defaults")
        return DEFAULT_ALERT_CONFIG.copy()

    def save_config(self, cfg=None):
        """Save alert configuration to disk."""
        with self._lock:
            if cfg is not None:
                self._config = cfg
            ALERT_CONFIG_FILE.write_text(
                json.dumps(self._config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def get_config(self):
        """Return current config (safe copy)."""
        with self._lock:
            return json.loads(json.dumps(self._config))

    # ---- Health check execution ----

    def run_checks(self):
        """Run all health checks and evaluate rules. Returns list of triggered alerts."""
        triggered = []
        config = self.get_config()
        if not config.get("enabled"):
            return triggered

        now = time.time()
        rules = config.get("rules", {})
        resources = self.system.get_resources()

        # 1. CPU check
        if rules.get("cpu_high", {}).get("enabled"):
            rule = rules["cpu_high"]
            cpu = resources.get("cpu_avg_1m", 0)
            if cpu > rule["threshold"]:
                duration_needed = rule.get("duration", 120)
                key = "cpu_high"
                if key not in self._sustained:
                    self._sustained[key] = now
                elif now - self._sustained[key] >= duration_needed:
                    msg = rule["message"].format(
                        threshold=rule["threshold"], duration=duration_needed, current=cpu
                    )
                    triggered.append(self._fire("cpu_high", rule["severity"], msg))
                    self._sustained.pop(key, None)
            else:
                self._sustained.pop("cpu_high", None)

        # 2. Memory check
        if rules.get("memory_high", {}).get("enabled"):
            rule = rules["memory_high"]
            mem = resources.get("memory_percent", 0)
            if mem > rule["threshold"]:
                duration_needed = rule.get("duration", 120)
                key = "memory_high"
                if key not in self._sustained:
                    self._sustained[key] = now
                elif now - self._sustained[key] >= duration_needed:
                    msg = rule["message"].format(
                        threshold=rule["threshold"], duration=duration_needed, current=mem
                    )
                    triggered.append(self._fire("memory_high", rule["severity"], msg))
                    self._sustained.pop(key, None)
            else:
                self._sustained.pop("memory_high", None)

        # 3. Disk check
        if rules.get("disk_low", {}).get("enabled"):
            rule = rules["disk_low"]
            low, free_pct = self.system.check_disk_space(rule["threshold"])
            if low:
                msg = rule["message"].format(threshold=rule["threshold"], current=round(free_pct, 1))
                triggered.append(self._fire("disk_low", rule["severity"], msg))

        # 4. LLM error rate
        if self.metrics and rules.get("llm_error_rate", {}).get("enabled"):
            rule = rules["llm_error_rate"]
            stats = self.metrics.get_stats()
            total = stats.get("llm_calls", 0)
            errors = stats.get("llm_errors", 0)
            if total >= rule.get("min_calls", 5):
                rate = (errors / total) * 100
                if rate > rule["threshold"]:
                    msg = rule["message"].format(threshold=rule["threshold"], current=round(rate, 1))
                    triggered.append(self._fire("llm_error_rate", rule["severity"], msg))

        # 5. LLM latency
        if self.metrics and rules.get("llm_latency", {}).get("enabled"):
            rule = rules["llm_latency"]
            stats = self.metrics.get_stats()
            total = stats.get("llm_calls", 0)
            avg_ms = stats.get("llm_avg_duration_ms", 0)
            if total >= rule.get("min_calls", 3) and avg_ms > rule["threshold"]:
                msg = rule["message"].format(threshold=rule["threshold"], current=avg_ms)
                triggered.append(self._fire("llm_latency", rule["severity"], msg))

        # 6. LLM unreachable (check each enabled backend)
        if rules.get("llm_unreachable", {}).get("enabled"):
            rule = rules["llm_unreachable"]
            # Import config module from app
            try:
                sys.path.insert(0, str(Path(__file__).parent.parent))
                import importlib
                config_mod = importlib.import_module("app")
                backends = getattr(config_mod, "config", {}).get("llms", {}).get("backends", [])
                for b in backends:
                    if b.get("enabled", True):
                        ok, err = self.llm_checker.check_backend(b)
                        if not ok:
                            msg = rule["message"].format(error=err)
                            triggered.append(self._fire(f"llm_unreachable_{b.get('name', 'unknown')}", rule["severity"], msg))
            except Exception:
                pass  # Can't import app config, skip

        # 7. Login failures
        if self.metrics and rules.get("login_failures", {}).get("enabled"):
            rule = rules["login_failures"]
            stats = self.metrics.get_stats()
            failures = stats.get("login_failures", 0)
            if failures >= rule["threshold"]:
                msg = rule["message"].format(current=failures)
                triggered.append(self._fire("login_failures", rule["severity"], msg))

        return triggered

    def _fire(self, rule_name, severity, message):
        """Record alert and send notifications."""
        entry = self.history.add(rule_name, severity, message)
        if not entry:
            return None  # Deduplicated

        logger.warning("ALERT [%s] %s: %s", severity.upper(), rule_name, message)

        config = self.get_config()
        notif = config.get("notifications", {})

        # In-app (always logged to history, visible in API)
        # Webhook
        if notif.get("webhook", {}).get("enabled"):
            send_webhook(notif["webhook"], f"AI Chat Alert - {rule_name}", message, severity)

        # Email
        if notif.get("email", {}).get("enabled"):
            send_email(notif["email"], f"Alert: {rule_name}", message, severity)

        return entry

    # ---- Scheduler ----

    def start(self, interval=None):
        """Start periodic health checks."""
        if self._running:
            return
        self._running = True
        interval = interval or self._config.get("check_interval", 60)
        self._timer = threading.Timer(interval, self._run_periodic)
        self._timer.daemon = True
        self._timer.start()
        logger.info("Alert engine started (interval=%ds)", interval)

    def stop(self):
        """Stop periodic health checks."""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        logger.info("Alert engine stopped")

    def _run_periodic(self):
        if not self._running:
            return
        try:
            self.run_checks()
        except Exception:
            logger.exception("Alert check failed")
        # Schedule next
        interval = self._config.get("check_interval", 60)
        self._timer = threading.Timer(interval, self._run_periodic)
        self._timer.daemon = True
        self._timer.start()

    # ---- Convenience ----

    def get_status(self):
        """Return current monitoring status summary."""
        resources = self.system.get_resources()
        recent = self.history.get_recent(10)
        return {
            "enabled": self._config.get("enabled", True),
            "check_interval": self._config.get("check_interval", 60),
            "resources": resources,
            "recent_alerts": recent,
            "active_rules": {
                k: {"enabled": v.get("enabled", False), "severity": v.get("severity", "info")}
                for k, v in self._config.get("rules", {}).items()
            },
        }


# ============================================================
# CLI
# ============================================================
def main():
    import argparse

    parser = argparse.ArgumentParser(description="AI Chat Alert Engine")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check", help="Run health checks now")
    sub.add_parser("status", help="Show current status")
    p_history = sub.add_parser("history", help="Show recent alerts")
    p_history.add_argument("-n", "--limit", type=int, default=20)
    sub.add_parser("test-webhook", help="Send a test webhook")

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    engine = AlertEngine()

    if args.command == "check":
        alerts = engine.run_checks()
        if alerts:
            print(f"\n{len(alerts)} alert(s) triggered:")
            for a in alerts:
                print(f"  [{a['severity'].upper()}] {a['message']}")
        else:
            print("\nAll checks passed - no alerts.")

    elif args.command == "status":
        status = engine.get_status()
        r = status["resources"]
        print(f"\nAlert Engine: {'ON' if status['enabled'] else 'OFF'}")
        print(f"Check Interval: {status['check_interval']}s")
        print(f"\nSystem Resources:")
        print(f"  CPU:    {r.get('cpu_percent', '-')}% (avg: {r.get('cpu_avg_1m', '-')}%)")
        print(f"  Memory: {r.get('memory_percent', '-')}% ({r.get('memory_used_mb', '-')}/{r.get('memory_total_mb', '-')} MB)")
        print(f"  Disk:   {r.get('disk_percent', '-')}% used ({r.get('disk_free_gb', '-')} GB free)")
        print(f"\nActive Rules:")
        for name, rule in status["active_rules"].items():
            state = "ON" if rule["enabled"] else "OFF"
            print(f"  {name}: [{state}] severity={rule['severity']}")
        print(f"\nRecent Alerts:")
        for a in status["recent_alerts"]:
            print(f"  [{a['severity'].upper()}] {a['timestamp'][:19]} - {a['message']}")

    elif args.command == "history":
        entries = engine.history.get_recent(args.limit)
        if not entries:
            print("No alerts in history.")
        else:
            print(f"\n{'Time':<20} {'Sev':<10} {'Rule':<25} Message")
            print("-" * 90)
            for e in entries:
                print(f"{e['timestamp'][:19]:<20} {e['severity']:<10} {e['rule']:<25} {e['message'][:50]}")

    elif args.command == "test-webhook":
        config = engine.get_config()
        wh = config.get("notifications", {}).get("webhook", {})
        if not wh.get("enabled") or not wh.get("url"):
            print("Webhook not configured. Edit data/alerts/config.json first.")
        else:
            send_webhook(wh, "Test Alert", "This is a test notification from AI Chat.", "info")
            print("Test webhook sent. Check your notification channel.")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
