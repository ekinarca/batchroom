"""Authentication & authorization.

Two modes, switched by license + data:

* **Single-user (community / no users created):** exactly the v0.1 model —
  optional ``BATCHROOM_API_KEY`` guards everything; without it the server
  trusts its network (self-hosted default).
* **Multi-user (Pro license + ≥1 user):** cookie sessions and per-user API
  keys with admin/member roles. The env API key keeps working as an
  admin-equivalent so existing automations never break. If the license
  lapses, logins KEEP WORKING (never brick a server) — only user
  *management* pauses.
"""

from __future__ import annotations

import hmac
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..pro import entitlements, load_license
from ..pro.users import (
    authenticate,
    count_users,
    create_session,
    destroy_session,
    resolve_api_key,
    resolve_session,
)

SESSION_COOKIE = "br_session"


@dataclass
class Actor:
    kind: str  # open | env | user
    user: dict | None = None

    @property
    def is_admin(self) -> bool:
        if self.kind in ("open", "env"):
            return True
        return bool(self.user and self.user.get("role") == "admin")

    @property
    def user_id(self) -> int | None:
        return self.user.get("id") if self.user else None

    @property
    def label(self) -> str:
        if self.user:
            return self.user.get("email", "user")
        return self.kind


# ---------------------------------------------------------------------------
# license/feature helpers (cached on app.state)
# ---------------------------------------------------------------------------


def refresh_license(app) -> None:
    app.state.license_info = load_license(app.state.settings)
    app.state.__dict__.pop("_user_count_cache", None)


def license_info(app):
    return app.state.license_info


def features(app) -> set[str]:
    return entitlements(app.state.license_info)


def _user_count(app) -> int:
    cache = app.state.__dict__.get("_user_count_cache")
    now = time.monotonic()
    if cache and now - cache[1] < 3.0:
        return cache[0]
    count = count_users(app.state.db)
    app.state.__dict__["_user_count_cache"] = (count, now)
    return count


def invalidate_user_cache(app) -> None:
    app.state.__dict__.pop("_user_count_cache", None)


def multiuser_active(app) -> bool:
    """Users exist AND the license grants multi_user (valid, incl. grace).

    A lapsed license keeps sessions working via ``users_exist`` fallback in
    get_actor — this flag governs *management* features.
    """
    return "multi_user" in features(app) and _user_count(app) > 0


def users_exist(app) -> bool:
    return _user_count(app) > 0


# ---------------------------------------------------------------------------
# actor resolution (global dependency for the protected API)
# ---------------------------------------------------------------------------


def _supplied_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    auth = request.headers.get("authorization", "")
    if not key and auth.lower().startswith("bearer "):
        key = auth[7:].strip()
    return key or request.query_params.get("api_key")


def get_actor(request: Request) -> Actor:
    app = request.app
    settings = app.state.settings
    supplied = _supplied_key(request)

    # env key is always an admin passe-partout (automation continuity)
    if settings.api_key and supplied and hmac.compare_digest(
        supplied.encode("utf-8"), settings.api_key.encode("utf-8")
    ):
        actor = Actor(kind="env")
        request.state.actor = actor
        return actor

    if users_exist(app):
        user = resolve_session(app.state.db, request.cookies.get(SESSION_COOKIE))
        if user is None:
            user = resolve_api_key(app.state.db, supplied)
        if user is not None:
            actor = Actor(kind="user", user=user)
            request.state.actor = actor
            return actor
        raise HTTPException(
            status_code=401,
            detail={"detail": "login required", "login_required": True},
        )

    # single-user mode
    if settings.api_key:
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    actor = Actor(kind="open")
    request.state.actor = actor
    return actor


def require_admin(request: Request) -> Actor:
    actor = getattr(request.state, "actor", None) or get_actor(request)
    if not actor.is_admin:
        raise HTTPException(status_code=403, detail="admin role required")
    return actor


def actor_of(request: Request) -> Actor:
    return getattr(request.state, "actor", None) or Actor(kind="open")


# ---------------------------------------------------------------------------
# login rate limiting (in-memory, per source)
# ---------------------------------------------------------------------------


@dataclass
class _RateLimiter:
    """Sliding-window counter of *failed* attempts per key.

    Memory stays bounded without ever wiping live buckets: wiping the whole
    table once it filled up let an attacker reset their own counter by
    spraying a thousand throwaway keys."""

    max_attempts: int = 5
    window_seconds: float = 60.0
    max_keys: int = 10000
    buckets: OrderedDict = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _recent(self, key: str, now: float) -> deque | None:
        bucket = self.buckets.get(key)
        if bucket is None:
            return None
        while bucket and now - bucket[0] > self.window_seconds:
            bucket.popleft()
        if not bucket:
            del self.buckets[key]
            return None
        return bucket

    def reserve(self, key: str) -> float | None:
        """Check and count an attempt in one step, so a burst of concurrent
        requests can't all pass the check before any failure is recorded.
        Returns the attempt's stamp (hand it to ``release`` if the attempt
        turns out not to be a failure), or None when the key is blocked."""
        now = time.monotonic()
        with self._lock:
            bucket = self._recent(key, now)
            if bucket is not None and len(bucket) >= self.max_attempts:
                return None
            if bucket is None:
                bucket = self.buckets[key] = deque()
            bucket.append(now)
            self.buckets.move_to_end(key)
            while len(self.buckets) > self.max_keys:  # evict least recently failing
                self.buckets.popitem(last=False)
            return now

    def release(self, key: str, stamp: float) -> None:
        with self._lock:
            bucket = self.buckets.get(key)
            if bucket is None:
                return
            try:
                bucket.remove(stamp)
            except ValueError:
                return
            if not bucket:
                del self.buckets[key]


# 5 failures/min per account+source; 30/min per source across all accounts,
# so spraying many emails from one address is throttled as well.
_login_limiter = _RateLimiter()
_source_limiter = _RateLimiter(max_attempts=30)


# ---------------------------------------------------------------------------
# open routes: login / logout / me (the UI boots against these)
# ---------------------------------------------------------------------------

router = APIRouter()


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


@router.post("/auth/login")
def login(request: Request, response: Response, body: LoginBody) -> dict:
    app = request.app
    if not users_exist(app):
        raise HTTPException(status_code=400, detail="multi-user mode is not set up")
    client = request.client.host if request.client else "?"
    account_key = f"{client}:{body.email.strip().lower()}"
    # Count the attempt as a failure up front and take it back on success:
    # checking first and counting after the slow password hash let a burst
    # of concurrent requests through before the first failure was recorded.
    source_stamp = _source_limiter.reserve(client)
    if source_stamp is None:
        raise HTTPException(status_code=429, detail="too many attempts — wait a minute")
    account_stamp = _login_limiter.reserve(account_key)
    if account_stamp is None:
        _source_limiter.release(client, source_stamp)
        raise HTTPException(status_code=429, detail="too many attempts — wait a minute")
    user = authenticate(app.state.db, body.email, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid email or password")
    _login_limiter.release(account_key, account_stamp)
    _source_limiter.release(client, source_stamp)
    token = create_session(app.state.db, user["id"])
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"user": user, "authenticated": True}


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict:
    destroy_session(request.app.state.db, request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"authenticated": False}


@router.get("/auth/me")
def me(request: Request) -> dict:
    """Auth state probe — open on purpose so the UI knows what to render."""
    app = request.app
    info = license_info(app)
    state = {
        "multiuser": users_exist(app),
        "license": info.to_public_dict(),
        "auth_required": bool(app.state.settings.api_key) or users_exist(app),
    }
    try:
        actor = get_actor(request)
    except HTTPException:
        return {**state, "authenticated": False, "user": None, "role": None}
    return {
        **state,
        "authenticated": True,
        "user": actor.user,
        "role": "admin" if actor.is_admin else "member",
        "kind": actor.kind,
    }
