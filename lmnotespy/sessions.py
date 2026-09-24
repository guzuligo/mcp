"""Persistent SQLite session store for lmnotespy.

Session IDs are 12-digit local timestamps (YYMMDDHHMMSS). On collision,
the ID is incremented by 1 until a vacant slot is found (bounded retry).

DB location: $TMPDIR/lmnotes/sessions.db, overridable via LMNOTES_SESSION_DB.

Schema::

    sessions(id TEXT PRIMARY KEY, folder TEXT NOT NULL, created_at TEXT NOT NULL)
    state(key TEXT PRIMARY KEY, value TEXT)   -- tracks current session id
"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import tempfile

# ---------------------------------------------------------------------------
# DB path
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = Path(tempfile.gettempdir()) / "lmnotes" / "sessions.db"
_SESSION_DB_ENV = "LMNOTES_SESSION_DB"


def _resolve_db_path() -> Path:
    env = os.environ.get(_SESSION_DB_ENV)
    if env:
        return Path(env)
    return _DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Return a connection to the sessions database, creating tables if needed."""
    path = db_path or _resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          TEXT PRIMARY KEY,
            folder      TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    return conn


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def _local_timestamp() -> str:
    """Return current local time as YYMMDDHHMMSS."""
    now = datetime.now()
    return now.strftime("%y%m%d%H%M%S")


def generate_session_id(conn: Optional[sqlite3.Connection] = None) -> str:
    """Generate a unique 12-digit session ID with conflict retry.

    On collision the ID is incremented by 1 until vacant, bounded to ~1000 retries.
    """
    if conn is None:
        conn = _get_conn()
    ts = _local_timestamp()
    for offset in range(1001):  # 0..1000
        candidate = str(int(ts) + offset).zfill(12)[-12:]  # keep 12 digits
        row = conn.execute("SELECT 1 FROM sessions WHERE id=?", (candidate,)).fetchone()
        if row is None:
            return candidate
    raise RuntimeError("Unable to generate unique session ID after 1001 attempts")



# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_session(folder: str, conn=None):
    """Create a new session row. Returns {id, folder, created_at}."""
    if conn is None:
        conn = _get_conn()
    sid = generate_session_id(conn)
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO sessions (id, folder, created_at) VALUES (?, ?, ?)",
        (sid, folder, now),
    )
    conn.commit()
    return {"id": sid, "folder": folder, "created_at": now}


def get_session(sid: str, conn=None):
    """Return session dict or None."""
    if conn is None:
        conn = _get_conn()
    row = conn.execute("SELECT id, folder, created_at FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None:
        return None
    return {"id": row[0], "folder": row[1], "created_at": row[2]}


def list_sessions(conn=None):
    """Return all sessions ordered by id."""
    if conn is None:
        conn = _get_conn()
    rows = conn.execute("SELECT id, folder, created_at FROM sessions ORDER BY id").fetchall()
    return [{"id": r[0], "folder": r[1], "created_at": r[2]} for r in rows]


def delete_session(sid: str, conn=None):
    """Delete a session. Returns True if it existed."""
    if conn is None:
        conn = _get_conn()
    cur = conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit()
    return cur.rowcount > 0


def list_session_ids(conn=None):
    """Return set of all session IDs."""
    if conn is None:
        conn = _get_conn()
    rows = conn.execute("SELECT id FROM sessions").fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Current-session tracking (state table)
# ---------------------------------------------------------------------------

_CURRENT_KEY = "current_session_id"


def set_current_session(sid: str, conn=None) -> None:
    """Persist the current session ID."""
    if conn is None:
        conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        (_CURRENT_KEY, sid),
    )
    conn.commit()


def get_current_session(conn=None):
    """Return the current session ID or None."""
    if conn is None:
        conn = _get_conn()
    row = conn.execute("SELECT value FROM state WHERE key=?", (_CURRENT_KEY,)).fetchone()
    return row[0] if row else None


def clear_current_session(conn=None) -> None:
    """Remove the current-session marker."""
    if conn is None:
        conn = _get_conn()
    conn.execute("DELETE FROM state WHERE key=?", (_CURRENT_KEY,))
    conn.commit()


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def get_current_session_info(conn=None):
    """Return current session dict or None."""
    sid = get_current_session(conn)
    if sid is None:
        return None
    return get_session(sid, conn)
