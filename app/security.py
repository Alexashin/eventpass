import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def verify_csrf(request: Request, provided: str) -> None:
    expected = request.session.get("csrf_token")
    if not expected or not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Некорректный CSRF-токен")


class LoginLimiter:
    def __init__(self, max_failures: int, window_seconds: int):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._entries: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._entries[key]
        border = now - self.window_seconds
        while q and q[0] < border:
            q.popleft()
        return q

    def allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return len(self._prune(key, now)) < self.max_failures

    def fail(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(key, now).append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)
