"""
Cancellation support for agent runs.

Two modes:
  - Direct cancel: the run is aborted immediately at the next loop
    checkpoint (used for plain chat turns).
  - Confirm cancel: the run enters "cancel_pending" state and waits for
    the human to approve the cancellation (used for task mode runs where
    tool side effects are already in flight). Only after approval does the
    run actually stop.

The manager is a process-wide singleton shared between the Flask request
thread (which sets cancel signals) and the agent loop thread (which polls).
"""

import threading
import time

# Cancellation states
NONE = "none"             # no cancellation requested
PENDING = "pending"       # cancellation requested, awaiting human approval
APPROVED = "approved"     # human approved the cancellation -> stop now
DENIED = "denied"         # human denied the cancellation -> keep going

# Client-initiated cancel requests: "direct" vs "confirm"
DIRECT = "direct"
CONFIRM = "confirm"

DEFAULT_EXPIRY_SECONDS = 120
POLL_INTERVAL = 0.2


class CancellationRequest:
    """A single in-flight cancellation request for one agent run."""

    def __init__(self, mode: str = DIRECT, expiry_seconds: float = DEFAULT_EXPIRY_SECONDS):
        self.mode = mode
        self.status = PENDING
        self.reason = ""
        self.created_at = time.time()
        self.decided_at: float | None = None
        self.expiry_seconds = expiry_seconds
        self._lock = threading.Lock()

    def _expired(self) -> bool:
        return (time.time() - self.created_at) >= self.expiry_seconds

    def approve(self) -> str:
        """Approve cancellation -> status APPROVED. Returns new status."""
        with self._lock:
            if self.status == PENDING and not self._expired():
                self.status = APPROVED
                self.decided_at = time.time()
            return self.status

    def deny(self, reason: str = "") -> str:
        """Deny cancellation -> status DENIED. Returns new status."""
        with self._lock:
            if self.status == PENDING and not self._expired():
                self.status = DENIED
                self.decided_at = time.time()
                self.reason = reason
            return self.status

    def check(self) -> str:
        """Return the current status; auto-expires stale pending requests."""
        with self._lock:
            if self.status == PENDING and self._expired():
                self.status = DENIED  # expired -> treated as "no cancel"
                self.reason = "确认超时，已继续执行"
            return self.status


class CancellationManager:
    """Tracks cancellation requests per run_id (thread-safe)."""

    def __init__(self, poll_interval: float = POLL_INTERVAL):
        self._requests: dict[str, CancellationRequest] = {}
        self._lock = threading.Lock()
        self.poll_interval = poll_interval

    def request(self, run_id: str, mode: str = DIRECT,
                expiry_seconds: float = DEFAULT_EXPIRY_SECONDS) -> CancellationRequest:
        """Create (or reuse) a cancellation request for a run."""
        with self._lock:
            req = self._requests.get(run_id)
            if req is None or req.status != PENDING:
                req = CancellationRequest(mode=mode, expiry_seconds=expiry_seconds)
                self._requests[run_id] = req
            return req

    def get(self, run_id: str) -> CancellationRequest | None:
        with self._lock:
            req = self._requests.get(run_id)
            if req is None:
                return None
            if req.status == PENDING and req._expired():
                req.check()  # auto-expire -> DENIED
            return req

    def approve(self, run_id: str) -> bool:
        """Approve a pending cancellation. Returns True if approved."""
        req = self.get(run_id)
        return bool(req and req.approve() == APPROVED)

    def deny(self, run_id: str, reason: str = "") -> bool:
        """Deny a pending cancellation. Returns True if denied."""
        req = self.get(run_id)
        return bool(req and req.deny(reason) == DENIED)

    def clear(self, run_id: str) -> None:
        """Drop the request for a run (called when the run finishes)."""
        with self._lock:
            self._requests.pop(run_id, None)

    def should_stop(self, run_id: str | None) -> bool:
        """Agent-loop checkpoint: should the current run abort now?

        - no request / no run_id -> False
        - DIRECT mode pending/approved -> True (abort immediately)
        - CONFIRM mode -> only stop when APPROVED (human said yes);
          pending/denied -> keep going.
        """
        if not run_id:
            return False
        req = self.get(run_id)
        if req is None:
            return False
        if req.mode == DIRECT:
            return req.check() in (PENDING, APPROVED)
        return req.check() == APPROVED


# Singleton shared by the Flask app and the orchestrator
manager = CancellationManager()
