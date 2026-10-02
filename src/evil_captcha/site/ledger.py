"""Ledger: visitor sessions, test runs, and the activity log.

A session is the visitor's unit: one browser, identified by a cookie token.
Tasks and verification belong to the session. Sessions live in memory and are
bounded: idle ones expire, and beyond a limit the least recently used is dropped.

A pass token is what passing the captcha yields: the widget hands it to the page
it is embedded in, whose server redeems it once, within ``PASS_TTL_S``. Like
sessions, pass tokens live in memory only and are bounded.

A run is the test harness's unit: everything from one client IP registered by
the harness. Only sessions from registered IPs belong to a run, and only runs
keep their events in memory, so public traffic cannot grow it.

Every event of every session is also written to the ``evil_captcha.activity``
logger as one JSON object with the session id. Visitors stay anonymous: the log
holds neither client IPs nor the names on certificates. Written to a file, it
rotates daily and keeps ``ACTIVITY_RETENTION_DAYS`` days.
"""

import json
import logging
import logging.handlers
import secrets
import sys
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evil_captcha.tasks import Task

activity = logging.getLogger("evil_captcha.activity")

SESSION_TTL_S = 3600.0
MAX_SESSIONS = 100_000
PASS_TTL_S = 300.0
MAX_PASSES = 100_000
ACTIVITY_RETENTION_DAYS = 30


def log_activity(path: Path | None) -> None:
    """Send the activity log to the file (or stdout) as bare JSON lines, not to the root logger.

    A file rotates at midnight UTC, and files older than the retention are deleted.
    """
    handler: logging.Handler
    if path:
        handler = logging.handlers.TimedRotatingFileHandler(
            path, when="midnight", utc=True, backupCount=ACTIVITY_RETENTION_DAYS - 1
        )
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    activity.addHandler(handler)
    activity.setLevel(logging.INFO)
    activity.propagate = False


@dataclass(frozen=True)
class Challenge:
    id: str
    task: Task


@dataclass
class Run:
    id: str
    client_ip: str
    template_id: str | None = None  # fixed task for the whole run; None draws randomly
    sessions: list[Session] = field(default_factory=list["Session"])
    certificates: list[str] = field(default_factory=list[str])  # certified statements
    events: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    @property
    def passed(self) -> bool:
        return any(session.passed for session in self.sessions)

    def report(self) -> dict[str, Any]:
        return {
            "run_id": self.id,
            "client_ip": self.client_ip,
            "template_id": self.template_id,
            "passed": self.passed,
            "sessions": len(self.sessions),
            "certificates": list(self.certificates),
            "events": list(self.events),
        }


@dataclass
class Session:
    id: str  # for logs; the token itself is a credential and never logged
    token: str
    run: Run | None = None  # set only for IPs registered by the test harness
    challenge: Challenge | None = None
    passed: bool = False
    certificate: str | None = None  # the one certificate a pass earns, once issued
    last_seen: float = 0.0

    def record(self, event_type: str, **data: Any) -> None:
        event = {"type": event_type, "at": datetime.now(UTC).isoformat(), "session": self.id}
        event |= data
        activity.info(json.dumps(event, ensure_ascii=False))
        if self.run:
            self.run.events.append(event)


class Ledger:
    """Maps cookie tokens to sessions and registered client IPs to runs."""

    def __init__(
        self,
        clock: Callable[[], float],
        session_ttl_s: float = SESSION_TTL_S,
        max_sessions: int = MAX_SESSIONS,
        pass_ttl_s: float = PASS_TTL_S,
        max_passes: int = MAX_PASSES,
    ) -> None:
        self._clock = clock
        self._session_ttl_s = session_ttl_s
        self._max_sessions = max_sessions
        self._runs: dict[str, Run] = {}
        self._run_by_ip: dict[str, Run] = {}
        self._sessions: OrderedDict[str, Session] = OrderedDict()  # least recently used first
        self._pass_ttl_s = pass_ttl_s
        self._max_passes = max_passes
        # pass token -> (session, issue time), oldest first
        self._passes: OrderedDict[str, tuple[Session, float]] = OrderedDict()
        self.lock = threading.Lock()

    def register(self, client_ip: str, template_id: str | None = None) -> Run:
        """Start a fresh test run for sessions from this IP."""
        run = Run(id=uuid.uuid4().hex, client_ip=client_ip, template_id=template_id)
        self._runs[run.id] = run
        self._run_by_ip[client_ip] = run
        return run

    def session(self, token: str | None, client_ip: str) -> Session:
        """The session for this token, or a new one if it is unknown or expired."""
        now = self._clock()
        self._expire(now)
        session = self._sessions.get(token) if token else None
        if session is None:
            session = Session(
                id=uuid.uuid4().hex[:12],
                token=secrets.token_urlsafe(32),
                run=self._run_by_ip.get(client_ip),
            )
            if session.run:
                session.run.sessions.append(session)
            self._sessions[session.token] = session
            if len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
        session.last_seen = now
        self._sessions.move_to_end(session.token)
        return session

    def issue_pass(self, session: Session) -> str:
        """A new pass token for a session that passed the captcha."""
        token = secrets.token_urlsafe(32)
        self._passes[token] = (session, self._clock())
        if len(self._passes) > self._max_passes:
            self._passes.popitem(last=False)
        return token

    def redeem(self, token: str) -> Session | None:
        """The session a pass token was issued to, once; None if unknown, used or expired."""
        now = self._clock()
        while self._passes:
            _, issued = next(iter(self._passes.values()))
            if now - issued <= self._pass_ttl_s:
                break
            self._passes.popitem(last=False)
        entry = self._passes.pop(token, None)
        return entry[0] if entry else None

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def _expire(self, now: float) -> None:
        while self._sessions:
            oldest = next(iter(self._sessions.values()))
            if now - oldest.last_seen <= self._session_ttl_s:
                break
            self._sessions.popitem(last=False)


def new_challenge_id() -> str:
    return secrets.token_urlsafe(12)
