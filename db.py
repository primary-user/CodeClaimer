"""
db.py — SQLite storage for CodeClaimer.

Rules this file follows:
  * Never drop or rewrite existing data. Migrations only ADD tables, columns
    and indexes, so every card already posted keeps working.
  * A copy of the database is taken on every boot, before migrations run.
  * Every function is synchronous and finishes without awaiting, so a
    check-then-write sequence inside one function can never interleave with
    another interaction. That is what makes claims race safe.
"""
import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

log = logging.getLogger("codeclaimer.db")

DB_PATH = os.getenv("DB_PATH", "data/codes.db")
BACKUPS_KEPT = 2

_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

DEFAULT_SETTINGS = {
    "mods_only": 1,
    "one_claim_per_batch": 1,
    "claim_verification": 1,
    "verification_mode": "easy",
    "dot_start_seconds": 60,
    "claim_roles": "",
    "claim_cooldown_minutes": 0,
    # Resurfacing (all inert until a mod picks channels). See resurface.py.
    "resurface_channels": "",
    "resurface_activity": "off",
    "resurface_schedule": "off",
    "last_call": 1,
    "raffle_reminder": 1,
    "mod_channel_id": 0,
    "cleanup_60d": 0,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_db() -> None:
    """
    Copy the live database to <db dir>/backups/ using SQLite's online backup
    API (safe while WAL is active). Keeps the newest BACKUPS_KEPT copies and
    skips the copy if the volume is short on space.
    """
    if not os.path.exists(DB_PATH):
        log.info("Backup skipped: no database yet at %s", DB_PATH)
        return
    try:
        size = os.path.getsize(DB_PATH)
        backup_dir = os.path.join(os.path.dirname(DB_PATH) or ".", "backups")
        os.makedirs(backup_dir, exist_ok=True)
        free = shutil.disk_usage(backup_dir).free
        if free < size * 3 + 20 * 1024 * 1024:
            log.warning("Backup skipped: only %.1f MB free for a %.1f MB database",
                        free / 1e6, size / 1e6)
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = os.path.join(backup_dir, f"codes-{stamp}.db")
        src = sqlite3.connect(DB_PATH, timeout=10)
        dst = sqlite3.connect(target)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()

        copies = sorted(
            f for f in os.listdir(backup_dir) if f.startswith("codes-") and f.endswith(".db")
        )
        for old in copies[:-BACKUPS_KEPT]:
            os.remove(os.path.join(backup_dir, old))
        log.info("Database backed up to %s (%.1f MB)", target, size / 1e6)
    except Exception:
        log.exception("Database backup failed; continuing without one")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shared_codes (
                message_id   INTEGER PRIMARY KEY,
                product_code TEXT NOT NULL,
                item_name    TEXT NOT NULL,
                platform     TEXT NOT NULL DEFAULT '',
                expires_at   TEXT NOT NULL DEFAULT '',
                guild_id     INTEGER,
                channel_id   INTEGER,
                sharer_id    INTEGER,
                sharer_name  TEXT,
                created_at   TEXT,
                batch_id     TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id             INTEGER PRIMARY KEY,
                mods_only            INTEGER NOT NULL DEFAULT 1,
                one_claim_per_batch  INTEGER NOT NULL DEFAULT 1,
                claim_verification   INTEGER NOT NULL DEFAULT 1,
                verification_mode    TEXT    NOT NULL DEFAULT 'easy',
                dot_start_seconds    INTEGER NOT NULL DEFAULT 60
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS batch_claims (
                batch_id   TEXT NOT NULL,
                user_id    INTEGER NOT NULL,
                guild_id   INTEGER NOT NULL,
                claimed_at TEXT,
                PRIMARY KEY (batch_id, user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claim_cooldowns (
                user_id    INTEGER NOT NULL,
                guild_id   INTEGER NOT NULL,
                claimed_at TEXT NOT NULL,
                PRIMARY KEY (user_id, guild_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claims_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id     INTEGER NOT NULL,
                channel_id   INTEGER,
                message_id   INTEGER,
                claimer_id   INTEGER NOT NULL,
                claimer_name TEXT    NOT NULL,
                item_name    TEXT    NOT NULL,
                platform     TEXT    NOT NULL DEFAULT '',
                sharer_id    INTEGER,
                sharer_name  TEXT,
                claimed_at   TEXT    NOT NULL
            )
        """)

        # Forward migrations. ADD only, never drop.
        _add_columns(conn, "shared_codes", [
            ("guild_id",    "INTEGER"),
            ("channel_id",  "INTEGER"),
            ("sharer_id",   "INTEGER"),
            ("sharer_name", "TEXT"),
            ("created_at",  "TEXT"),
            ("expires_at",  "TEXT NOT NULL DEFAULT ''"),
            ("batch_id",    "TEXT"),
            ("card_style",  "TEXT NOT NULL DEFAULT 'legacy'"),
            ("resurface_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_call_msg",   "INTEGER"),
            ("is_test",         "INTEGER NOT NULL DEFAULT 0"),
        ])
        _add_columns(conn, "guild_settings", [
            ("one_claim_per_batch",    "INTEGER NOT NULL DEFAULT 1"),
            ("claim_verification",     "INTEGER NOT NULL DEFAULT 1"),
            ("verification_mode",      "TEXT NOT NULL DEFAULT 'easy'"),
            ("dot_start_seconds",      "INTEGER NOT NULL DEFAULT 60"),
            ("claim_roles",            "TEXT NOT NULL DEFAULT ''"),
            ("claim_cooldown_minutes", "INTEGER NOT NULL DEFAULT 0"),
            ("resurface_channels",     "TEXT NOT NULL DEFAULT ''"),
            ("resurface_activity",     "TEXT NOT NULL DEFAULT 'off'"),
            ("resurface_schedule",     "TEXT NOT NULL DEFAULT 'off'"),
            ("last_call",              "INTEGER NOT NULL DEFAULT 1"),
            ("raffle_reminder",        "INTEGER NOT NULL DEFAULT 1"),
            ("mod_channel_id",         "INTEGER NOT NULL DEFAULT 0"),
            ("cleanup_60d",            "INTEGER NOT NULL DEFAULT 0"),
        ])
        # product_code is kept so the winner can see their code again from the
        # card. Mods never see it in /claimlog. expires_at is kept for the same
        # reveal. Rows logged before this column existed simply have NULL.
        _add_columns(conn, "claims_log", [
            ("product_code", "TEXT"),
            ("expires_at",   "TEXT"),
            ("source",       "TEXT NOT NULL DEFAULT 'drop'"),
        ])

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_shared_codes_expires_at
            ON shared_codes (expires_at)
            WHERE expires_at IS NOT NULL AND expires_at != ''
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_batch_claims_batch ON batch_claims (batch_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_log_guild ON claims_log (guild_id, claimed_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_log_claimer ON claims_log (guild_id, claimer_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_log_message ON claims_log (message_id, claimer_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_shared_codes_guild ON shared_codes (guild_id)")
        # Lifetime counters. codes_removed counts codes that left without a
        # claim (expired, withdrawn, card deleted, cleared), so the total of
        # codes ever dropped survives their rows being deleted.
        conn.execute("CREATE TABLE IF NOT EXISTS bot_stats (key TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT OR IGNORE INTO bot_stats (key, value) VALUES ('codes_removed', 0)")
        conn.commit()


def _add_columns(conn: sqlite3.Connection, table: str, cols: list[tuple[str, str]]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, definition in cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            log.info("Migration: added %s.%s", table, col)


# ---------------------------------------------------------------------------
# Guild settings
# ---------------------------------------------------------------------------

_INSERT_DEFAULTS = """
    INSERT OR IGNORE INTO guild_settings
        (guild_id, mods_only, one_claim_per_batch, claim_verification,
         verification_mode, dot_start_seconds, claim_roles, claim_cooldown_minutes)
    VALUES (?, 1, 1, 1, 'easy', 60, '', 0)
"""

_SETTING_COLUMNS = frozenset(DEFAULT_SETTINGS)


def seed_guild_settings(guild_ids: list[int]) -> None:
    if not guild_ids:
        return
    with get_db() as conn:
        conn.executemany(_INSERT_DEFAULTS, [(gid,) for gid in guild_ids])
        conn.commit()


def get_settings(guild_id: int) -> dict:
    """All settings for a guild as a plain dict. Creates the default row if missing."""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT mods_only, one_claim_per_batch, claim_verification,
                   verification_mode, dot_start_seconds, claim_roles, claim_cooldown_minutes,
                   resurface_channels, resurface_activity, resurface_schedule, last_call,
                   raffle_reminder, mod_channel_id, cleanup_60d
            FROM guild_settings WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()
        if row is None:
            conn.execute(_INSERT_DEFAULTS, (guild_id,))
            conn.commit()
            s = dict(DEFAULT_SETTINGS)
            s["claim_roles"], s["resurface_channels"] = [], []
            return s
    s = dict(row)
    for key in ("claim_roles", "resurface_channels"):
        try:
            s[key] = [int(x) for x in json.loads(s[key])] if s[key] else []
        except (ValueError, TypeError):
            s[key] = []
    s["mod_channel_id"] = int(s["mod_channel_id"] or 0)
    s["dot_start_seconds"] = int(s["dot_start_seconds"] or 60)
    s["claim_cooldown_minutes"] = int(s["claim_cooldown_minutes"] or 0)
    return s


def set_setting(guild_id: int, column: str, value) -> None:
    if column not in _SETTING_COLUMNS:
        raise ValueError(f"Unknown setting {column}")
    if column in ("claim_roles", "resurface_channels"):
        value = json.dumps(value) if value else ""
    with get_db() as conn:
        conn.execute(_INSERT_DEFAULTS, (guild_id,))
        conn.execute(f"UPDATE guild_settings SET {column} = ? WHERE guild_id = ?", (value, guild_id))
        conn.commit()


# ---------------------------------------------------------------------------
# Shared codes
# ---------------------------------------------------------------------------

def insert_code(
    message_id: int, product_code: str, item_name: str, platform: str, expires_at: str,
    guild_id: int | None, channel_id: int | None, sharer_id: int | None,
    sharer_name: str, batch_id: str, card_style: str, is_test: bool = False,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO shared_codes
                (message_id, product_code, item_name, platform, expires_at,
                 guild_id, channel_id, sharer_id, sharer_name, created_at, batch_id, card_style, is_test)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, product_code, item_name, platform, expires_at, guild_id, channel_id,
             sharer_id, sharer_name, now_iso(), batch_id or None, card_style, 1 if is_test else 0),
        )
        conn.commit()


def get_code(message_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM shared_codes WHERE message_id = ?", (message_id,)).fetchone()
    return dict(row) if row else None


def delete_code(message_id: int) -> dict | None:
    """Remove a code row and return it, or None if it was already gone."""
    with get_db() as conn:
        row = conn.execute(
            "DELETE FROM shared_codes WHERE message_id = ? RETURNING *", (message_id,)
        ).fetchone()
        if row is not None and not row["is_test"]:
            conn.execute("UPDATE bot_stats SET value = value + 1 WHERE key = 'codes_removed'")
        conn.commit()
    return dict(row) if row else None


def codes_with_expiry() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT message_id, item_name, platform, expires_at, channel_id, sharer_name, card_style
            FROM shared_codes WHERE expires_at IS NOT NULL AND expires_at != ''
            """
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Claim gates and the atomic claim
# ---------------------------------------------------------------------------

def has_claimed_from_batch(batch_id: str, user_id: int) -> bool:
    if not batch_id:
        return False
    with get_db() as conn:
        return conn.execute(
            "SELECT 1 FROM batch_claims WHERE batch_id = ? AND user_id = ?", (batch_id, user_id)
        ).fetchone() is not None


def cooldown_remaining(user_id: int, guild_id: int, cooldown_minutes: int) -> float:
    if cooldown_minutes <= 0:
        return 0.0
    with get_db() as conn:
        row = conn.execute(
            "SELECT claimed_at FROM claim_cooldowns WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        ).fetchone()
    if row is None:
        return 0.0
    try:
        end = datetime.fromisoformat(row["claimed_at"]) + timedelta(minutes=cooldown_minutes)
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError):
        return 0.0


def claim_code(
    message_id: int, user_id: int, user_name: str, enforce_batch: bool,
) -> tuple[str, dict | None]:
    """
    The one place a code changes hands. Runs as a single transaction:
      1. the code row must still exist
      2. if one claim per batch applies, the user must not hold one already
      3. delete the code row, record batch claim, cooldown, and claims log
    Returns ("ok", row) or ("gone" | "batch", None).
    """
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM shared_codes WHERE message_id = ?", (message_id,)).fetchone()
        if row is None:
            conn.rollback()
            return "gone", None
        row = dict(row)
        batch_id = row.get("batch_id") or ""
        guild_id = row.get("guild_id")

        if enforce_batch and batch_id:
            held = conn.execute(
                "SELECT 1 FROM batch_claims WHERE batch_id = ? AND user_id = ?", (batch_id, user_id)
            ).fetchone()
            if held:
                conn.rollback()
                return "batch", None

        ts = now_iso()
        conn.execute("DELETE FROM shared_codes WHERE message_id = ?", (message_id,))
        if batch_id and guild_id:
            conn.execute(
                "INSERT OR IGNORE INTO batch_claims (batch_id, user_id, guild_id, claimed_at) VALUES (?, ?, ?, ?)",
                (batch_id, user_id, guild_id, ts),
            )
        if guild_id:
            conn.execute(
                "INSERT OR REPLACE INTO claim_cooldowns (user_id, guild_id, claimed_at) VALUES (?, ?, ?)",
                (user_id, guild_id, ts),
            )
            conn.execute(
                """
                INSERT INTO claims_log
                    (guild_id, channel_id, message_id, claimer_id, claimer_name, item_name,
                     platform, sharer_id, sharer_name, claimed_at, product_code, expires_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (guild_id, row.get("channel_id"), message_id, user_id, user_name,
                 row["item_name"], row.get("platform") or "", row.get("sharer_id"),
                 row.get("sharer_name"), ts, row["product_code"], row.get("expires_at") or "",
                 "test" if row.get("is_test") else "drop"),
            )
        conn.commit()
        return "ok", row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_claim_for_user(message_id: int, user_id: int) -> dict | None:
    """The claim a user made on a card, if any, newest first. Used by Show my code."""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT item_name, platform, product_code, expires_at, claimed_at, sharer_name
            FROM claims_log WHERE message_id = ? AND claimer_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (message_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def get_claim_log(guild_id: int, limit: int = 30, claimer_id: int | None = None,
                  item_filter: str | None = None) -> list[dict]:
    query = """
        SELECT id, claimer_id, claimer_name, item_name, platform, sharer_name,
               channel_id, message_id, claimed_at, source
        FROM claims_log WHERE guild_id = ?
    """
    params: list = [guild_id]
    if claimer_id is not None:
        query += " AND claimer_id = ?"
        params.append(claimer_id)
    if item_filter:
        query += " AND item_name LIKE ?"
        params.append(f"%{item_filter}%")
    query += " ORDER BY claimed_at DESC LIMIT ?"
    params.append(limit)
    with get_db() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


# ---------------------------------------------------------------------------
# Stats (for logs, so usage is visible in Railway)
# ---------------------------------------------------------------------------

def usage_stats() -> dict:
    now = datetime.now(timezone.utc)
    d7  = (now - timedelta(days=7)).isoformat()
    d30 = (now - timedelta(days=30)).isoformat()
    with get_db() as conn:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        stats = {
            "live_codes":      q("SELECT COUNT(*) FROM shared_codes WHERE is_test = 0"),
            "live_code_guilds": q("SELECT COUNT(DISTINCT guild_id) FROM shared_codes"),
            "claims_total":    q("SELECT COUNT(*) FROM claims_log WHERE source != 'test'"),
            "claims_7d":       q("SELECT COUNT(*) FROM claims_log WHERE source != 'test' AND claimed_at >= ?", d7),
            "claims_30d":      q("SELECT COUNT(*) FROM claims_log WHERE source != 'test' AND claimed_at >= ?", d30),
            "claimers_30d":    q("SELECT COUNT(DISTINCT claimer_id) FROM claims_log WHERE source != 'test' AND claimed_at >= ?", d30),
            "guilds_active_30d": q("SELECT COUNT(DISTINCT guild_id) FROM claims_log WHERE source != 'test' AND claimed_at >= ?", d30),
        }
        try:
            stats["raffles_open"] = q("SELECT COUNT(*) FROM raffles WHERE status = 'open'")
        except sqlite3.OperationalError:
            pass
    return stats


def live_codes_in_guild(guild_id: int) -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM shared_codes WHERE guild_id = ?", (guild_id,)).fetchone()[0]


def live_code_breakdown(limit: int = 12) -> list[dict]:
    """Unclaimed codes per guild with the age of the oldest, biggest first."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT guild_id, COUNT(*) AS n, MIN(created_at) AS oldest, MAX(created_at) AS newest,
                   SUM(CASE WHEN expires_at != '' THEN 1 ELSE 0 END) AS with_expiry,
                   COUNT(DISTINCT channel_id) AS channels
            FROM shared_codes GROUP BY guild_id ORDER BY n DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def codes_dropped_total() -> int:
    """Every code ever shared: open now + claimed from drops + raffle prizes + removed unclaimed.
    Codes removed before the counter existed (Nov 2026) are not in it."""
    with get_db() as conn:
        q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        total = q("SELECT COUNT(*) FROM shared_codes WHERE is_test = 0")
        total += q("SELECT COUNT(*) FROM claims_log WHERE source = 'drop'")
        total += q("SELECT COALESCE(SUM(value), 0) FROM bot_stats WHERE key = 'codes_removed'")
        try:
            total += q("SELECT COUNT(*) FROM raffle_codes")
        except sqlite3.OperationalError:
            pass
    return total
