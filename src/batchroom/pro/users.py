"""Multi-user accounts, sessions and per-user API keys (Pro).

Passwords: scrypt (stdlib), per-user salt, format ``scrypt$N$r$p$salt$hash``.
Sessions: random 256-bit tokens; only their SHA-256 lands in SQLite, so a
database leak can't be replayed as cookies. Same for API keys (``brk_…``).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from ..core.errors import BatchroomError
from ..queue.db import Database, row_to_user

SESSION_DAYS = 30
API_KEY_PREFIX = "brk_"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 16384, 8, 1


class UserError(BatchroomError):
    """Invalid user operation (duplicate email, weak password, ...)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise UserError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p),
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


def count_users(db: Database) -> int:
    with db.connect() as conn:
        return conn.execute("SELECT count(*) FROM users").fetchone()[0]


def count_admins(db: Database) -> int:
    with db.connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM users WHERE role='admin' AND disabled=0"
        ).fetchone()[0]


def list_users(db: Database) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [row_to_user(r) for r in rows]


def get_user(db: Database, user_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return row_to_user(row) if row else None


def create_user(
    db: Database,
    *,
    email: str,
    name: str = "",
    password: str,
    role: str = "member",
    seat_limit: int | None = None,
) -> dict:
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise UserError(f"invalid email address: {email!r}")
    if role not in ("admin", "member"):
        raise UserError("role must be 'admin' or 'member'")
    if seat_limit is not None and count_users(db) >= seat_limit:
        raise UserError(
            f"seat limit reached ({seat_limit})",
            hint="your license covers this many users; contact sales for more seats",
        )
    stored = hash_password(password)
    import sqlite3

    try:
        with db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO users (email, name, password, role, created_at)"
                " VALUES (?,?,?,?,?)",
                (email, name.strip(), stored, role, _iso(_now())),
            )
            row = conn.execute(
                "SELECT * FROM users WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise UserError(f"a user with email {email} already exists") from exc
    return row_to_user(row)


def update_user(
    db: Database,
    user_id: int,
    *,
    role: str | None = None,
    password: str | None = None,
    disabled: bool | None = None,
    name: str | None = None,
) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise UserError(f"user {user_id} not found")
        if role is not None and role != row["role"]:
            if role not in ("admin", "member"):
                raise UserError("role must be 'admin' or 'member'")
            if row["role"] == "admin" and _is_last_admin(conn, user_id):
                raise UserError("cannot demote the last admin")
            conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        if disabled is not None and bool(disabled) != bool(row["disabled"]):
            if disabled and row["role"] == "admin" and _is_last_admin(conn, user_id):
                raise UserError("cannot disable the last admin")
            conn.execute(
                "UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, user_id)
            )
            if disabled:
                conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        if name is not None:
            conn.execute("UPDATE users SET name=? WHERE id=?", (name.strip(), user_id))
        if password is not None:
            stored = hash_password(password)
            conn.execute("UPDATE users SET password=? WHERE id=?", (stored, user_id))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        updated = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return row_to_user(updated)


def delete_user(db: Database, user_id: int) -> None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise UserError(f"user {user_id} not found")
        if row["role"] == "admin" and _is_last_admin(conn, user_id):
            raise UserError("cannot delete the last admin")
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        # jobs keep their user_id for history; the UI shows 'deleted user'


def _is_last_admin(conn, user_id: int) -> bool:
    others = conn.execute(
        "SELECT count(*) FROM users WHERE role='admin' AND disabled=0 AND id != ?",
        (user_id,),
    ).fetchone()[0]
    return others == 0


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------


def authenticate(db: Database, email: str, password: str) -> dict | None:
    email = email.strip().lower()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email=? AND disabled=0", (email,)
        ).fetchone()
    if row is None:
        # burn comparable time so absent accounts aren't distinguishable
        verify_password(password, hash_password("timing-equalizer"))
        return None
    if not verify_password(password, row["password"]):
        return None
    return row_to_user(row)


def create_session(db: Database, user_id: int) -> str:
    raw = secrets.token_urlsafe(32)
    now = _now()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen)"
            " VALUES (?,?,?,?,?)",
            (_token_hash(raw), user_id, _iso(now), _iso(now + timedelta(days=SESSION_DAYS)),
             _iso(now)),
        )
        # opportunistic cleanup of expired sessions
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(now),))
    return raw


def resolve_session(db: Database, raw: str | None) -> dict | None:
    if not raw:
        return None
    now = _now()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash=? AND s.expires_at >= ? AND u.disabled=0",
            (_token_hash(raw), _iso(now)),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE sessions SET last_seen=? WHERE token_hash=?",
            (_iso(now), _token_hash(raw)),
        )
    return row_to_user(row)


def destroy_session(db: Database, raw: str | None) -> None:
    if not raw:
        return
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(raw),))


# ---------------------------------------------------------------------------
# per-user API keys
# ---------------------------------------------------------------------------


def rotate_api_key(db: Database, user_id: int) -> str:
    raw = API_KEY_PREFIX + secrets.token_urlsafe(24)
    with db.connect() as conn:
        cursor = conn.execute(
            "UPDATE users SET api_key_hash=? WHERE id=?", (_token_hash(raw), user_id)
        )
        if cursor.rowcount == 0:
            raise UserError(f"user {user_id} not found")
    return raw


def resolve_api_key(db: Database, raw: str | None) -> dict | None:
    if not raw or not raw.startswith(API_KEY_PREFIX):
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE api_key_hash=? AND disabled=0", (_token_hash(raw),)
        ).fetchone()
    return row_to_user(row) if row else None
