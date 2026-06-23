import os
import re
import json
import random
import asyncio
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("DISCORD_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")

DB_PATH = os.getenv("DB_PATH", "data/codes.db")

_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

TITLE_PHRASES = [
    "Free Code Available!",
    "Loot Drop Alert!",
    "A New Key Arrives!",
    "Claim This Code!",
    "Fresh Drop in the Channel!",
    "Spare Code Detected!",
    "Grab It While It's Hot!",
    "Community Gift Available!",
    "First Come First Served!",
    "New Code Up For Grabs!",
]

# In-memory store for pending math challenges.
# Key: (message_id, user_id) → correct answer (str)
# Short-lived — 60s timeout on the view means these clean themselves up.
_pending_challenges: dict[tuple[int, int], str] = {}

# ---------------------------------------------------------------------------
# Verification mode registry
# ---------------------------------------------------------------------------

VERIFICATION_MODES: dict[str, str] = {
    "easy":                 "Easy",
    "medium":               "Medium",
    "hard":                 "Hard",
    "difficulty_over_time": "Difficulty Over Time",
    "periodic":             "Periodic Table",
    "random":               "Random",
}

# Periodic table dataset — (symbol, element name)
PERIODIC_TABLE: list[tuple[str, str]] = [
    ("H",  "Hydrogen"),   ("He", "Helium"),     ("Li", "Lithium"),
    ("Be", "Beryllium"),  ("B",  "Boron"),       ("C",  "Carbon"),
    ("N",  "Nitrogen"),   ("O",  "Oxygen"),      ("F",  "Fluorine"),
    ("Ne", "Neon"),       ("Na", "Sodium"),      ("Mg", "Magnesium"),
    ("Al", "Aluminum"),   ("Si", "Silicon"),     ("P",  "Phosphorus"),
    ("S",  "Sulfur"),     ("Cl", "Chlorine"),    ("Ar", "Argon"),
    ("K",  "Potassium"),  ("Ca", "Calcium"),     ("Fe", "Iron"),
    ("Cu", "Copper"),     ("Zn", "Zinc"),        ("Ag", "Silver"),
    ("Au", "Gold"),       ("Hg", "Mercury"),     ("Pb", "Lead"),
    ("Sn", "Tin"),        ("Mn", "Manganese"),   ("Ni", "Nickel"),
    ("Co", "Cobalt"),     ("Cr", "Chromium"),    ("Ti", "Titanium"),
    ("Br", "Bromine"),    ("I",  "Iodine"),      ("Ba", "Barium"),
    ("Pt", "Platinum"),   ("W",  "Tungsten"),    ("Xe", "Xenon"),
    ("Ra", "Radium"),
]

# Difficulty Over Time configuration
DOT_TIERS    = ["hard", "hard_medium", "medium", "medium_easy", "easy"]
DOT_DROPOFF  = 0.75   # Each tier is 75% of the previous duration


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


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

        # Forward migrations — safe on existing databases, never drops data
        existing_codes = {row[1] for row in conn.execute("PRAGMA table_info(shared_codes)")}
        for col, definition in [
            ("guild_id",    "INTEGER"),
            ("channel_id",  "INTEGER"),
            ("sharer_id",   "INTEGER"),
            ("sharer_name", "TEXT"),
            ("created_at",  "TEXT"),
            ("expires_at",  "TEXT NOT NULL DEFAULT ''"),
            ("batch_id",    "TEXT"),
        ]:
            if col not in existing_codes:
                conn.execute(f"ALTER TABLE shared_codes ADD COLUMN {col} {definition}")

        existing_settings = {row[1] for row in conn.execute("PRAGMA table_info(guild_settings)")}
        for col, definition in [
            ("one_claim_per_batch",    "INTEGER NOT NULL DEFAULT 1"),
            ("claim_verification",     "INTEGER NOT NULL DEFAULT 1"),
            ("verification_mode",      "TEXT NOT NULL DEFAULT 'easy'"),
            ("dot_start_seconds",      "INTEGER NOT NULL DEFAULT 60"),
            ("claim_roles",            "TEXT NOT NULL DEFAULT ''"),
            ("claim_cooldown_minutes", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            if col not in existing_settings:
                conn.execute(f"ALTER TABLE guild_settings ADD COLUMN {col} {definition}")

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_shared_codes_expires_at
            ON shared_codes (expires_at)
            WHERE expires_at IS NOT NULL AND expires_at != ''
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_batch_claims_batch
            ON batch_claims (batch_id)
        """)

        conn.commit()


def seed_guild_settings(guild_ids: list[int]) -> None:
    """
    Insert a default row for any guild that does not already have one.
    INSERT OR IGNORE means existing settings are never overwritten on
    restart, redeploy, or DB wipe recovery.
    """
    if not guild_ids:
        return
    with get_db() as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO guild_settings
                (guild_id, mods_only, one_claim_per_batch, claim_verification,
                 verification_mode, dot_start_seconds, claim_roles, claim_cooldown_minutes)
            VALUES (?, 1, 1, 1, 'easy', 60, '', 0)
            """,
            [(gid,) for gid in guild_ids],
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(
    r"(https?://[^\s]+)|(www\.[^\s]+)|([a-zA-Z0-9-]+\.[a-zA-Z]{2,}(/[^\s]*)?)"
)
_JUNK_VALUES = frozenset({
    "unknown", "n/a", "na", "none", "no platform",
    "no expiration", "never", "no expiry", "no expire",
})


def contains_link(text: str) -> bool:
    return bool(_LINK_RE.search(text))


def clean_platform(platform: str | None) -> str:
    if not platform:
        return ""
    p = str(platform).strip()
    return "" if p.lower() in _JUNK_VALUES else p


def clean_expiration(expires_at: str | None) -> str:
    if not expires_at:
        return ""
    e = str(expires_at).strip()
    if e.lower() in _JUNK_VALUES:
        return ""
    e = re.sub(r"^exp(?:ires)?\s+", "", e, flags=re.IGNORECASE).strip()
    return e


# ---------------------------------------------------------------------------
# Expiration logic
# ---------------------------------------------------------------------------

_US_DATE_RE  = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:[ T](\d{1,2}):(\d{2}))?")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?")


def parse_expiration_datetime(expires_at: str | None) -> datetime | None:
    e = clean_expiration(expires_at)
    if not e:
        return None

    for pattern, order in [
        (_US_DATE_RE,  ("month", "day", "year")),
        (_ISO_DATE_RE, ("year", "month", "day")),
    ]:
        m = pattern.search(e)
        if not m:
            continue
        try:
            if order == ("month", "day", "year"):
                month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))

            if m.group(4) and m.group(5):
                return datetime(year, month, day, int(m.group(4)), int(m.group(5)),
                                tzinfo=timezone.utc)
            return datetime(year, month, day, tzinfo=timezone.utc) + timedelta(days=1)
        except ValueError:
            return None

    return None


def expiration_is_valid(expires_at: str | None) -> bool:
    e = clean_expiration(expires_at)
    return (not e) or (parse_expiration_datetime(e) is not None)


def is_expired(expires_at: str | None) -> bool:
    dt = parse_expiration_datetime(expires_at)
    return dt is not None and datetime.now(timezone.utc) >= dt


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def format_platform_line(platform: str | None) -> str:
    p = clean_platform(platform)
    return f"**Platform:** {p}\n" if p else ""


def format_expiration_line(expires_at: str | None) -> str:
    e = clean_expiration(expires_at)
    return f"**Expires:** {e}\n" if e else ""


def build_expired_embed(item_name: str, platform: str, expires_at: str) -> discord.Embed:
    suffix = f" ({clean_platform(platform)})" if clean_platform(platform) else ""
    return discord.Embed(
        title="Code Expired",
        description=(
            f"The code for **{item_name}**{suffix} was not claimed before it expired.\n\n"
            f"**Expired:** {clean_expiration(expires_at)}"
        ),
        color=discord.Color.dark_grey(),
    )


def build_claimed_embed(
    item_name: str,
    platform: str,
    claimer_display: str,
    sharer_display: str,
) -> discord.Embed:
    suffix = f" ({clean_platform(platform)})" if clean_platform(platform) else ""
    return discord.Embed(
        title="Loot Claimed!",
        description=(
            f"The code for **{item_name}**{suffix} has been claimed by **{claimer_display}**!\n\n"
            f"Thank you to {sharer_display} for sharing with the community!"
        ),
        color=discord.Color.dark_grey(),
    )


def build_already_claimed_embed() -> discord.Embed:
    return discord.Embed(
        title="Already Claimed",
        description=(
            "This code has already been claimed by someone else.\n\n"
            "Keep an eye out for future drops!"
        ),
        color=discord.Color.dark_grey(),
    )


# ---------------------------------------------------------------------------
# Challenge generators
# ---------------------------------------------------------------------------

def _numeric_decoys(correct: int, lo: int, hi: int, count: int = 3) -> list[int]:
    """Generate plausible wrong answers near correct within [lo, hi]."""
    pool: set[int] = set()
    offsets = [o for o in range(-20, 21) if o != 0]
    random.shuffle(offsets)
    for o in offsets:
        d = correct + o
        if lo <= d <= hi and d != correct:
            pool.add(d)
        if len(pool) >= count:
            break
    while len(pool) < count:
        d = random.randint(lo, hi)
        if d != correct:
            pool.add(d)
    return list(pool)[:count]


def generate_easy_challenge() -> tuple[str, str, list[str]]:
    """Simple addition or subtraction — answer is always 1–9."""
    while True:
        op = random.choice(["add", "sub"])
        if op == "add":
            a = random.randint(1, 8)
            b = random.randint(1, 9 - a)
            answer = a + b
            question = f"{a} + {b}"
        else:
            a = random.randint(2, 9)
            b = random.randint(1, a - 1)
            answer = a - b
            question = f"{a} − {b}"
        if 1 <= answer <= 9:
            pool = [n for n in range(1, 10) if n != answer]
            choices = [str(answer)] + [str(d) for d in random.sample(pool, 3)]
            random.shuffle(choices)
            return question, str(answer), choices


def generate_medium_easy_challenge() -> tuple[str, str, list[str]]:
    """Single operation — answer is 10–49. DOT intermediate tier."""
    for _ in range(50):
        op = random.choice(["add", "sub"])
        if op == "add":
            a = random.randint(5, 40)
            b = random.randint(5, 49 - a)
            ans = a + b
            if 10 <= ans <= 49:
                q = f"{a} + {b}"
                choices = [str(ans)] + [str(d) for d in _numeric_decoys(ans, 10, 49)]
                random.shuffle(choices)
                return q, str(ans), choices
        else:
            a = random.randint(15, 70)
            b = random.randint(1, a - 10)
            ans = a - b
            if 10 <= ans <= 49:
                q = f"{a} − {b}"
                choices = [str(ans)] + [str(d) for d in _numeric_decoys(ans, 10, 49)]
                random.shuffle(choices)
                return q, str(ans), choices
    return generate_easy_challenge()


def generate_medium_challenge() -> tuple[str, str, list[str]]:
    """PEMDAS expression — integer answer 10–99."""
    for _ in range(100):
        pattern = random.choice(["add_mul", "mul_sub", "paren_add_mul", "two_products"])
        ans, q = None, None
        if pattern == "add_mul":
            a, b, c = random.randint(1, 15), random.randint(2, 9), random.randint(2, 9)
            ans, q = a + b * c, f"{a} + {b} × {c}"
        elif pattern == "mul_sub":
            a, b = random.randint(2, 9), random.randint(2, 9)
            c = random.randint(1, 30)
            ans, q = a * b - c, f"{a} × {b} − {c}"
        elif pattern == "paren_add_mul":
            a, b, c = random.randint(1, 9), random.randint(1, 9), random.randint(2, 9)
            ans, q = (a + b) * c, f"({a} + {b}) × {c}"
        elif pattern == "two_products":
            a, b = random.randint(2, 7), random.randint(2, 7)
            c, d = random.randint(2, 7), random.randint(2, 7)
            ans, q = a * b + c * d, f"{a} × {b} + {c} × {d}"
        if ans is not None and 10 <= ans <= 99:
            choices = [str(ans)] + [str(d) for d in _numeric_decoys(ans, 10, 99)]
            random.shuffle(choices)
            return q, str(ans), choices
    return generate_easy_challenge()


def generate_hard_medium_challenge() -> tuple[str, str, list[str]]:
    """Multi-step PEMDAS with nested parentheses — answer 10–99. DOT intermediate tier."""
    for _ in range(100):
        pattern = random.choice(["nested", "two_paren", "paren_sub_mul"])
        ans, q = None, None
        if pattern == "nested":
            a, b, c, d = (random.randint(1, 9), random.randint(2, 9),
                          random.randint(2, 9), random.randint(1, 15))
            ans, q = (a + b * c) - d, f"({a} + {b} × {c}) − {d}"
        elif pattern == "two_paren":
            a, b = random.randint(2, 7), random.randint(1, 7)
            c, d = random.randint(2, 7), random.randint(1, 7)
            e = random.randint(2, 5)
            ans, q = (a + b) * e - c * d, f"({a} + {b}) × {e} − {c} × {d}"
        elif pattern == "paren_sub_mul":
            a, b, c = random.randint(5, 15), random.randint(1, 5), random.randint(2, 8)
            ans, q = (a - b) * c, f"({a} − {b}) × {c}"
        if ans is not None and 10 <= ans <= 99:
            choices = [str(ans)] + [str(d) for d in _numeric_decoys(ans, 10, 99)]
            random.shuffle(choices)
            return q, str(ans), choices
    return generate_medium_challenge()


def generate_hard_challenge() -> tuple[str, str, list[str]]:
    """Algebraic equation — solve for x. Answer is 1–50."""
    for _ in range(100):
        form = random.choice(["linear_add", "linear_sub", "div_add", "combined"])
        ans, q = None, None
        if form == "linear_add":
            x = random.randint(1, 15)
            a, b = random.randint(2, 9), random.randint(1, 20)
            c = a * x + b
            ans, q = x, f"{a}x + {b} = {c}, solve for x"
        elif form == "linear_sub":
            x = random.randint(1, 15)
            a, b = random.randint(2, 9), random.randint(1, 20)
            c = a * x - b
            if c < 1:
                continue
            ans, q = x, f"{a}x − {b} = {c}, solve for x"
        elif form == "div_add":
            a = random.randint(2, 5)
            x_div = random.randint(1, 10)
            x = x_div * a
            b = random.randint(1, 10)
            c = x_div + b
            ans, q = x, f"x ÷ {a} + {b} = {c}, solve for x"
        elif form == "combined":
            x = random.randint(1, 12)
            a, b = random.randint(2, 8), random.randint(2, 8)
            c = (a + b) * x
            ans, q = x, f"{a}x + {b}x = {c}, solve for x"
        if ans is not None and 1 <= ans <= 50:
            choices = [str(ans)] + [str(d) for d in _numeric_decoys(ans, 1, 50)]
            random.shuffle(choices)
            return q, str(ans), choices
    return generate_medium_challenge()


def generate_periodic_challenge() -> tuple[str, str, list[str]]:
    """Periodic table — identify the symbol for a named element."""
    correct_sym, correct_name = random.choice(PERIODIC_TABLE)
    pool = [(sym, name) for sym, name in PERIODIC_TABLE if sym != correct_sym]
    decoys = [sym for sym, _ in random.sample(pool, 3)]
    choices = [correct_sym] + decoys
    random.shuffle(choices)
    q = f"Which symbol represents the element **{correct_name}**?"
    return q, correct_sym, choices


# ---------------------------------------------------------------------------
# Difficulty Over Time helpers
# ---------------------------------------------------------------------------

def _dot_tier_durations(start_seconds: int) -> list[float | None]:
    """
    Returns per-tier durations for the 5 DOT tiers.
    The final entry is None — Easy is the floor and holds indefinitely.
    """
    durations: list[float | None] = []
    t = float(start_seconds)
    for _ in range(4):          # Hard, Hard-Medium, Medium, Medium-Easy
        durations.append(t)
        t = t * DOT_DROPOFF
    durations.append(None)      # Easy floor
    return durations


def _dot_tier_for_elapsed(elapsed_seconds: float, start_seconds: int) -> str:
    """Return the active DOT tier name given elapsed seconds since the drop was posted."""
    durations = _dot_tier_durations(start_seconds)
    accumulated = 0.0
    for i, dur in enumerate(durations):
        if dur is None:
            return DOT_TIERS[i]   # Easy, holds indefinitely
        accumulated += dur
        if elapsed_seconds < accumulated:
            return DOT_TIERS[i]
    return "easy"


def _generate_for_tier(tier: str) -> tuple[str, str, list[str]]:
    if tier == "hard":
        return generate_hard_challenge()
    elif tier == "hard_medium":
        return generate_hard_medium_challenge()
    elif tier == "medium":
        return generate_medium_challenge()
    elif tier == "medium_easy":
        return generate_medium_easy_challenge()
    else:
        return generate_easy_challenge()


def generate_challenge(
    mode: str,
    elapsed_seconds: float = 0.0,
    dot_start_seconds: int = 60,
) -> tuple[str, str, list[str]]:
    """
    Generate a (question, correct_answer, shuffled_choices) tuple
    for the given verification mode.
    """
    if mode == "easy":
        return generate_easy_challenge()
    elif mode == "medium":
        return generate_medium_challenge()
    elif mode == "hard":
        return generate_hard_challenge()
    elif mode == "difficulty_over_time":
        tier = _dot_tier_for_elapsed(elapsed_seconds, dot_start_seconds)
        return _generate_for_tier(tier)
    elif mode == "periodic":
        return generate_periodic_challenge()
    elif mode == "random":
        picked = random.choice(["easy", "medium", "hard", "periodic"])
        return generate_challenge(picked)
    else:
        return generate_easy_challenge()


# ---------------------------------------------------------------------------
# Bulk parsing helpers
# ---------------------------------------------------------------------------

def parse_bulk_label(label: str) -> tuple[str, str]:
    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", label.strip())
    if m:
        return m.group(1).strip(), clean_platform(m.group(2))
    return label.strip(), ""


def parse_bulk_entry(entry: str) -> tuple[str, str, str, str] | None:
    entry = entry.strip()
    if ":" not in entry:
        return None

    raw_label, rest = entry.split(":", 1)
    raw_label, rest = raw_label.strip(), rest.strip()
    if not raw_label or not rest:
        return None

    if "|" in rest:
        product_code, expires_at = rest.rsplit("|", 1)
        product_code = product_code.strip()
        expires_at   = clean_expiration(expires_at)
    else:
        product_code, expires_at = rest.strip(), ""

    item_name, platform = parse_bulk_label(raw_label)
    if not item_name or not product_code:
        return None

    return item_name, platform, product_code, expires_at


def split_bulk_entries(raw_text: str) -> list[str]:
    if "\n" in raw_text:
        return [line.strip() for line in raw_text.splitlines() if line.strip()]
    if ";" in raw_text:
        return [e.strip() for e in raw_text.split(";") if e.strip()]
    return [raw_text.strip()] if raw_text.strip() else []


# ---------------------------------------------------------------------------
# Permission & settings helpers
# ---------------------------------------------------------------------------

def is_moderator(user) -> bool:
    perms = getattr(user, "guild_permissions", None)
    if perms is None:
        return False
    return perms.administrator or perms.manage_guild or perms.manage_messages


def _get_guild_settings(guild_id: int) -> sqlite3.Row:
    """
    Fetch guild settings, inserting defaults if no row exists yet.
    Uses INSERT OR IGNORE so existing settings are never clobbered.
    """
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT mods_only, one_claim_per_batch, claim_verification,
                   verification_mode, dot_start_seconds,
                   claim_roles, claim_cooldown_minutes
            FROM guild_settings WHERE guild_id = ?
            """,
            (guild_id,)
        ).fetchone()

        if row is None:
            conn.execute(
                """
                INSERT OR IGNORE INTO guild_settings
                    (guild_id, mods_only, one_claim_per_batch, claim_verification,
                     verification_mode, dot_start_seconds, claim_roles, claim_cooldown_minutes)
                VALUES (?, 1, 1, 1, 'easy', 60, '', 0)
                """,
                (guild_id,)
            )
            conn.commit()
            row = conn.execute(
                """
                SELECT mods_only, one_claim_per_batch, claim_verification,
                       verification_mode, dot_start_seconds,
                       claim_roles, claim_cooldown_minutes
                FROM guild_settings WHERE guild_id = ?
                """,
                (guild_id,)
            ).fetchone()

        return row


def get_mods_only(guild_id: int) -> bool:
    return bool(_get_guild_settings(guild_id)["mods_only"])


def get_one_claim_per_batch(guild_id: int) -> bool:
    return bool(_get_guild_settings(guild_id)["one_claim_per_batch"])


def get_claim_verification(guild_id: int) -> bool:
    return bool(_get_guild_settings(guild_id)["claim_verification"])


def _update_guild_setting(guild_id: int, column: str, value) -> None:
    """
    Generic upsert for a single guild_settings column.
    Only updates the target column — never overwrites others.
    """
    with get_db() as conn:
        # Ensure a row exists first
        conn.execute(
            """
            INSERT OR IGNORE INTO guild_settings
                (guild_id, mods_only, one_claim_per_batch, claim_verification,
                 verification_mode, dot_start_seconds, claim_roles, claim_cooldown_minutes)
            VALUES (?, 1, 1, 1, 'easy', 60, '', 0)
            """,
            (guild_id,)
        )
        conn.execute(
            f"UPDATE guild_settings SET {column} = ? WHERE guild_id = ?",
            (value, guild_id)
        )
        conn.commit()


def set_mods_only(guild_id: int, enabled: bool) -> None:
    _update_guild_setting(guild_id, "mods_only", 1 if enabled else 0)


def set_one_claim_per_batch(guild_id: int, enabled: bool) -> None:
    _update_guild_setting(guild_id, "one_claim_per_batch", 1 if enabled else 0)


def set_claim_verification(guild_id: int, enabled: bool) -> None:
    _update_guild_setting(guild_id, "claim_verification", 1 if enabled else 0)


def get_verification_mode(guild_id: int) -> str:
    mode = _get_guild_settings(guild_id)["verification_mode"]
    return mode if mode in VERIFICATION_MODES else "easy"


def set_verification_mode(guild_id: int, mode: str) -> None:
    _update_guild_setting(guild_id, "verification_mode", mode)


def get_dot_start_seconds(guild_id: int) -> int:
    val = _get_guild_settings(guild_id)["dot_start_seconds"]
    return int(val) if val else 60


def set_dot_start_seconds(guild_id: int, seconds: int) -> None:
    _update_guild_setting(guild_id, "dot_start_seconds", seconds)


def get_claim_roles(guild_id: int) -> list[int]:
    raw = _get_guild_settings(guild_id)["claim_roles"] or ""
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def set_claim_roles(guild_id: int, role_ids: list[int]) -> None:
    _update_guild_setting(guild_id, "claim_roles", json.dumps(role_ids) if role_ids else "")


def get_claim_cooldown_minutes(guild_id: int) -> int:
    val = _get_guild_settings(guild_id)["claim_cooldown_minutes"]
    return int(val) if val else 0


def set_claim_cooldown_minutes(guild_id: int, minutes: int) -> None:
    _update_guild_setting(guild_id, "claim_cooldown_minutes", minutes)


# ---------------------------------------------------------------------------
# Role gate helpers
# ---------------------------------------------------------------------------

def user_passes_role_gate(member: discord.Member, required_role_ids: list[int]) -> bool:
    """
    Returns True if the member is allowed to claim.
    No configured roles = open to all.
    Mods always bypass the role gate.
    """
    if not required_role_ids:
        return True
    if is_moderator(member):
        return True
    member_role_ids = {role.id for role in member.roles}
    return bool(member_role_ids & set(required_role_ids))


# ---------------------------------------------------------------------------
# Cooldown tracking
# ---------------------------------------------------------------------------

def get_cooldown_remaining(user_id: int, guild_id: int, cooldown_minutes: int) -> float:
    """Returns seconds remaining on cooldown, or 0.0 if no cooldown is active."""
    if cooldown_minutes <= 0:
        return 0.0
    with get_db() as conn:
        row = conn.execute(
            "SELECT claimed_at FROM claim_cooldowns WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id)
        ).fetchone()
    if row is None:
        return 0.0
    try:
        claimed_dt   = datetime.fromisoformat(row["claimed_at"])
        cooldown_end = claimed_dt + timedelta(minutes=cooldown_minutes)
        remaining    = (cooldown_end - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, remaining)
    except (ValueError, TypeError):
        return 0.0


def record_claim_cooldown(user_id: int, guild_id: int) -> None:
    """Record or refresh a user's claim timestamp for cooldown tracking."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO claim_cooldowns (user_id, guild_id, claimed_at)
            VALUES (?, ?, ?)
            """,
            (user_id, guild_id, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()


def format_cooldown_remaining(remaining_seconds: float) -> str:
    """Format seconds remaining as a human-readable string."""
    total = int(remaining_seconds)
    mins  = total // 60
    secs  = total % 60
    if mins > 0:
        return f"{mins} minute{'s' if mins != 1 else ''} and {secs} second{'s' if secs != 1 else ''}"
    return f"{secs} second{'s' if secs != 1 else ''}"


async def user_can_use_bot(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return True
    return (not get_mods_only(interaction.guild.id)) or is_moderator(interaction.user)


# ---------------------------------------------------------------------------
# Batch claim tracking
# ---------------------------------------------------------------------------

def has_claimed_from_batch(batch_id: str, user_id: int) -> bool:
    if not batch_id:
        return False
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM batch_claims WHERE batch_id = ? AND user_id = ?",
            (batch_id, user_id)
        ).fetchone()
        return row is not None


def record_batch_claim(batch_id: str, user_id: int, guild_id: int) -> None:
    if not batch_id:
        return
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO batch_claims (batch_id, user_id, guild_id, claimed_at)
            VALUES (?, ?, ?, ?)
            """,
            (batch_id, user_id, guild_id, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Core card actions
# ---------------------------------------------------------------------------

async def post_claim_card(
    channel,
    sharer,
    item_name: str,
    platform: str,
    product_code: str,
    expires_at: str = "",
    batch_id: str = "",
) -> discord.Message:
    platform   = clean_platform(platform)
    expires_at = clean_expiration(expires_at)

    embed = discord.Embed(
        title=random.choice(TITLE_PHRASES),
        description=(
            f"**Product:** {item_name}\n"
            f"{format_platform_line(platform)}"
            f"{format_expiration_line(expires_at)}"
            f"**Shared by:** {sharer.mention}\n\n"
            "Click the button below to claim it instantly via DM."
        ),
        color=discord.Color.gold(),
    )

    view = ClaimButtonView(
        product_code=product_code,
        item_name=item_name,
        platform=platform,
        expires_at=expires_at,
    )

    msg = await channel.send(embed=embed, view=view)

    sharer_name = getattr(sharer, "display_name", None) or getattr(sharer, "name", "Someone")

    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO shared_codes
                (message_id, product_code, item_name, platform, expires_at,
                 guild_id, channel_id, sharer_id, sharer_name, created_at, batch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg.id,
                product_code,
                item_name,
                platform,
                expires_at,
                getattr(getattr(channel, "guild", None), "id", None),
                getattr(channel, "id", None),
                getattr(sharer, "id", None),
                sharer_name,
                datetime.now(timezone.utc).isoformat(),
                batch_id or None,
            ),
        )
        conn.commit()

    return msg


async def mark_code_expired(
    message_id: int,
    item_name: str,
    platform: str,
    expires_at: str,
    channel_id: int | None,
) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM shared_codes WHERE message_id = ?", (message_id,))
        conn.commit()

    channel = bot.get_channel(channel_id) if channel_id else None
    if channel is None and channel_id:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            print(f"[Expiration] Cannot fetch channel {channel_id} for message {message_id}: {exc}")

    if channel is None:
        print(f"[Expiration] No channel for message {message_id} — DB row removed, card not updated.")
        return

    try:
        msg = await channel.fetch_message(message_id)
        await msg.edit(
            embed=build_expired_embed(item_name, platform, expires_at),
            view=None,
        )
        print(f"[Expiration] Marked message {message_id} as expired.")
    except discord.NotFound:
        print(f"[Expiration] Message {message_id} already deleted.")
    except discord.Forbidden:
        print(f"[Expiration] No permission to edit message {message_id} in channel {channel_id}.")
    except discord.HTTPException as exc:
        print(f"[Expiration] HTTP error on message {message_id}: {exc}")


# ---------------------------------------------------------------------------
# Views & Modals
# ---------------------------------------------------------------------------

class MathChallengeView(discord.ui.View):
    """
    Ephemeral view shown after clicking Claim Code when verification is ON.
    Presents a challenge question with 4 answer buttons.
    Times out after 60 seconds — card stays live for others.
    """

    def __init__(
        self,
        question: str,
        correct_answer: str,
        choices: list[str],
        message_id: int,
        code_to_send: str,
        item_to_send: str,
        platform_to_send: str,
        expires_to_send: str,
        sharer_display: str,
        batch_id: str,
        guild_id: int | None,
        channel_id: int | None,
    ):
        super().__init__(timeout=60)
        self.correct_answer   = correct_answer
        self.message_id       = message_id
        self.code_to_send     = code_to_send
        self.item_to_send     = item_to_send
        self.platform_to_send = platform_to_send
        self.expires_to_send  = expires_to_send
        self.sharer_display   = sharer_display
        self.batch_id         = batch_id
        self.guild_id         = guild_id
        self.channel_id       = channel_id
        self.answered         = False

        for choice in choices:
            btn = discord.ui.Button(
                label=str(choice),
                style=discord.ButtonStyle.secondary,
            )
            btn.callback = self._make_answer_callback(str(choice))
            self.add_item(btn)

    def _make_answer_callback(self, choice: str):
        async def callback(interaction: discord.Interaction):
            if self.answered:
                await interaction.response.send_message(
                    "You have already answered.", ephemeral=True
                )
                return

            if choice != self.correct_answer:
                await interaction.response.send_message(
                    "❌ Incorrect. Try clicking the claim button again.",
                    ephemeral=True,
                )
                self.stop()
                return

            # Correct — proceed with claim
            self.answered = True
            self.stop()

            # Atomic delete — only one concurrent answerer wins.
            # RETURNING gives us the row if we deleted it, None if someone else got there first.
            with get_db() as conn:
                won = conn.execute(
                    "DELETE FROM shared_codes WHERE message_id = ? RETURNING message_id",
                    (self.message_id,)
                ).fetchone()
                conn.commit()

            if not won:
                try:
                    msg = await interaction.channel.fetch_message(self.message_id)
                    await msg.edit(embed=build_already_claimed_embed(), view=None)
                except Exception:
                    pass
                await interaction.response.send_message(
                    "Someone else claimed this code while you were answering.",
                    ephemeral=True,
                )
                return

            display_platform = f" ({self.platform_to_send})" if self.platform_to_send else ""

            dm_embed = discord.Embed(
                title="🎁 Code Successfully Claimed!",
                description=(
                    f"Here is your activation key for **{self.item_to_send}**{display_platform}:"
                ),
                color=discord.Color.green(),
            )
            dm_embed.add_field(
                name="Product Code", value=f"`{self.code_to_send}`", inline=False
            )
            if self.expires_to_send:
                dm_embed.add_field(
                    name="Expires", value=self.expires_to_send, inline=False
                )
            dm_embed.add_field(
                name="Keep the cycle going!",
                value="Want secure code drops in your own community?\n[Vote or add on Top.gg](https://top.gg/bot/1511758084194832495)",
                inline=False,
            )

            try:
                await interaction.user.send(embed=dm_embed)
            except discord.Forbidden:
                # DM failed — put the row back so the code is not lost
                with get_db() as conn:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO shared_codes
                            (message_id, product_code, item_name, platform, expires_at,
                             guild_id, channel_id, created_at, batch_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self.message_id, self.code_to_send, self.item_to_send,
                            self.platform_to_send, self.expires_to_send,
                            self.guild_id, self.channel_id,
                            datetime.now(timezone.utc).isoformat(),
                            self.batch_id or None,
                        )
                    )
                    conn.commit()
                await interaction.response.send_message(
                    "Could not send you a DM. "
                    "Please open your Privacy Settings / DMs and try again.",
                    ephemeral=True,
                )
                return

            if self.batch_id and self.guild_id:
                record_batch_claim(self.batch_id, interaction.user.id, self.guild_id)

            if self.guild_id:
                record_claim_cooldown(interaction.user.id, self.guild_id)

            _pending_challenges.pop((self.message_id, interaction.user.id), None)

            await interaction.response.send_message(
                "✅ Correct! The code has been sent to your DMs.", ephemeral=True
            )

            try:
                msg = await interaction.channel.fetch_message(self.message_id)
                await msg.edit(
                    embed=build_claimed_embed(
                        item_name=self.item_to_send,
                        platform=self.platform_to_send,
                        claimer_display=interaction.user.display_name,
                        sharer_display=self.sharer_display,
                    ),
                    view=None,
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                print(f"[Claim] Could not update public card {self.message_id}: {exc}")

        return callback

    async def on_timeout(self):
        # Clean up the pending challenge entry on timeout
        # We don't have the user_id here but the in-memory dict is keyed on it.
        # Keys for this message_id are cleaned up when the user answers or on
        # the next successful claim. Small memory cost, harmless.
        pass


class ClaimButtonView(discord.ui.View):
    def __init__(
        self,
        product_code: str = None,
        item_name: str = None,
        platform: str = None,
        expires_at: str = None,
    ):
        super().__init__(timeout=None)
        self.product_code = product_code
        self.item_name    = item_name
        self.platform     = clean_platform(platform)
        self.expires_at   = clean_expiration(expires_at)

    @discord.ui.button(
        label="Claim Code 🎁",
        style=discord.ButtonStyle.green,
        custom_id="codeclaimer_claim_code_btn",
    )
    async def claim_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id   = interaction.message.id
        guild_id = interaction.guild.id if interaction.guild else None

        with get_db() as conn:
            row = conn.execute(
                """
                SELECT product_code, item_name, platform, expires_at,
                       channel_id, sharer_id, sharer_name, batch_id, created_at
                FROM shared_codes WHERE message_id = ?
                """,
                (msg_id,),
            ).fetchone()

        if row:
            code_to_send     = row["product_code"]
            item_to_send     = row["item_name"]
            platform_to_send = clean_platform(row["platform"])
            expires_to_send  = clean_expiration(row["expires_at"])
            channel_id       = row["channel_id"]
            sharer_id        = row["sharer_id"]
            sharer_name_db   = row["sharer_name"] or "Someone"
            batch_id         = row["batch_id"] or ""

            # Use stored display name — raw IDs do not resolve in embed descriptions
            sharer_display = (
                sharer_name_db
                if sharer_name_db and sharer_name_db != "Someone"
                else (f"<@{sharer_id}>" if sharer_id else "Someone")
            )
        else:
            # Stale card — clean it up
            try:
                await interaction.message.edit(embed=build_already_claimed_embed(), view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                print(f"[Claim] Could not clean up stale card {msg_id}: {exc}")
            await interaction.response.send_message(
                "This code has already been claimed!", ephemeral=True
            )
            return

        if not code_to_send:
            try:
                await interaction.message.edit(embed=build_already_claimed_embed(), view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                print(f"[Claim] Could not clean up empty card {msg_id}: {exc}")
            await interaction.response.send_message(
                "This code has already been claimed!", ephemeral=True
            )
            return

        if is_expired(expires_to_send):
            await mark_code_expired(
                message_id=msg_id,
                item_name=item_to_send,
                platform=platform_to_send,
                expires_at=expires_to_send,
                channel_id=channel_id,
            )
            await interaction.response.send_message(
                "This code was not claimed before it expired.", ephemeral=True
            )
            return

        # One claim per batch check
        if batch_id and guild_id and get_one_claim_per_batch(guild_id):
            if has_claimed_from_batch(batch_id, interaction.user.id):
                await interaction.response.send_message(
                    "You have already claimed a code from this batch. "
                    "One claim per batch drop is enabled on this server.",
                    ephemeral=True,
                )
                return

        # Role gate check
        if guild_id:
            required_roles = get_claim_roles(guild_id)
            if required_roles and not user_passes_role_gate(interaction.user, required_roles):
                await interaction.response.send_message(
                    "You don't have the required role to claim codes in this server.",
                    ephemeral=True,
                )
                return

        # Cooldown check
        if guild_id:
            cooldown_minutes = get_claim_cooldown_minutes(guild_id)
            if cooldown_minutes > 0:
                remaining = get_cooldown_remaining(interaction.user.id, guild_id, cooldown_minutes)
                if remaining > 0:
                    await interaction.response.send_message(
                        f"You're on cooldown. Try again in **{format_cooldown_remaining(remaining)}**.",
                        ephemeral=True,
                    )
                    return

        # Claim verification — show challenge if enabled
        if guild_id and get_claim_verification(guild_id):
            mode        = get_verification_mode(guild_id)
            dot_start   = get_dot_start_seconds(guild_id)

            # For DOT: derive elapsed seconds since the drop was posted
            elapsed = 0.0
            if mode == "difficulty_over_time":
                created_at_str = row["created_at"] if row else None
                if created_at_str:
                    try:
                        created_dt = datetime.fromisoformat(created_at_str)
                        elapsed = (datetime.now(timezone.utc) - created_dt).total_seconds()
                        elapsed = max(0.0, elapsed)
                    except (ValueError, TypeError):
                        elapsed = 0.0

            question, correct_answer, choices = generate_challenge(mode, elapsed, dot_start)
            _pending_challenges[(msg_id, interaction.user.id)] = correct_answer

            # Build a tier label for DOT so claimers can see current difficulty
            tier_label = ""
            if mode == "difficulty_over_time":
                tier = _dot_tier_for_elapsed(elapsed, dot_start)
                tier_display = {
                    "hard":        "Hard 🔴",
                    "hard_medium": "Hard-Medium 🟠",
                    "medium":      "Medium 🟡",
                    "medium_easy": "Medium-Easy 🟢",
                    "easy":        "Easy 🔵",
                }.get(tier, "")
                tier_label = f"\n*Difficulty: {tier_display}*"

            challenge_view = MathChallengeView(
                question=question,
                correct_answer=correct_answer,
                choices=choices,
                message_id=msg_id,
                code_to_send=code_to_send,
                item_to_send=item_to_send,
                platform_to_send=platform_to_send,
                expires_to_send=expires_to_send,
                sharer_display=sharer_display,
                batch_id=batch_id,
                guild_id=guild_id,
                channel_id=channel_id,
            )

            await interaction.response.send_message(
                f"**{question}**{tier_label}\n"
                "Answer within 60 seconds to claim your code.",
                view=challenge_view,
                ephemeral=True,
            )
            return

        # No verification — claim directly
        display_platform = f" ({platform_to_send})" if platform_to_send else ""

        dm_embed = discord.Embed(
            title="🎁 Code Successfully Claimed!",
            description=f"Here is your activation key for **{item_to_send}**{display_platform}:",
            color=discord.Color.green(),
        )
        dm_embed.add_field(name="Product Code", value=f"`{code_to_send}`", inline=False)
        if expires_to_send:
            dm_embed.add_field(name="Expires", value=expires_to_send, inline=False)
        dm_embed.add_field(
            name="Keep the cycle going!",
            value="Want secure code drops in your own community?\n[Vote or add on Top.gg](https://top.gg/bot/1511758084194832495)",
            inline=False,
        )

        # Atomic delete — only one caller gets a row back.
        # If another user claimed between the SELECT above and here, we get
        # None and turn away before sending any DM.
        with get_db() as conn:
            won = conn.execute(
                "DELETE FROM shared_codes WHERE message_id = ? RETURNING message_id",
                (msg_id,)
            ).fetchone()
            conn.commit()

        if not won:
            # Lost the race — someone else claimed it first
            try:
                await interaction.message.edit(embed=build_already_claimed_embed(), view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            await interaction.response.send_message(
                "This code was just claimed by someone else!", ephemeral=True
            )
            return

        # We won — now send the DM
        try:
            await interaction.user.send(embed=dm_embed)
        except discord.Forbidden:
            # DM failed — put the row back so the code is not lost
            with get_db() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO shared_codes
                        (message_id, product_code, item_name, platform, expires_at,
                         guild_id, channel_id, sharer_id, sharer_name, created_at, batch_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        msg_id, code_to_send, item_to_send, platform_to_send,
                        expires_to_send,
                        guild_id, channel_id, sharer_id, sharer_name_db,
                        datetime.now(timezone.utc).isoformat(),
                        batch_id or None,
                    )
                )
                conn.commit()
            await interaction.response.send_message(
                "Could not send you a DM. Please open your Privacy Settings / DMs and try again.",
                ephemeral=True,
            )
            return

        if batch_id and guild_id:
            record_batch_claim(batch_id, interaction.user.id, guild_id)

        if guild_id:
            record_claim_cooldown(interaction.user.id, guild_id)

        await interaction.response.send_message(
            "The code has been sent to your DMs!", ephemeral=True
        )

        try:
            await interaction.message.edit(
                embed=build_claimed_embed(
                    item_name=item_to_send,
                    platform=platform_to_send,
                    claimer_display=interaction.user.display_name,
                    sharer_display=sharer_display,
                ),
                view=None,
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            print(f"[Claim] Could not update public card {msg_id}: {exc}")


class DOTTimerModal(discord.ui.Modal, title="Difficulty Over Time — Starting Duration"):
    start_seconds = discord.ui.TextInput(
        label="Starting duration for Hard tier (seconds)",
        style=discord.TextStyle.short,
        required=True,
        min_length=1,
        max_length=4,
        placeholder="e.g. 60",
    )

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.start_seconds.value).strip()
        try:
            seconds = int(raw)
            if not (10 <= seconds <= 3600):
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Enter a whole number between 10 and 3600 seconds.",
                ephemeral=True,
            )
            return

        set_dot_start_seconds(self.guild_id, seconds)
        durations: list[float | None] = _dot_tier_durations(seconds)
        tier_str = " → ".join(
            f"{int(d)}s" if d is not None else "Easy (holds)"
            for d in durations
        )
        await interaction.response.send_message(
            f"✅ DOT start time set to **{seconds}s**.\n"
            f"Tier progression: {tier_str}",
            ephemeral=True,
        )


class CooldownModal(discord.ui.Modal, title="Claim Cooldown"):
    minutes = discord.ui.TextInput(
        label="Cooldown in minutes (0 = disabled)",
        style=discord.TextStyle.short,
        required=True,
        min_length=1,
        max_length=4,
        placeholder="e.g. 5  (0 to turn off)",
    )

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.minutes.value).strip()
        try:
            mins = int(raw)
            if not (0 <= mins <= 1440):
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Enter a whole number between 0 and 1440 minutes (0 = disabled).",
                ephemeral=True,
            )
            return
        set_claim_cooldown_minutes(self.guild_id, mins)
        if mins == 0:
            msg = "✅ Claim cooldown disabled."
        else:
            msg = f"✅ Claim cooldown set to **{mins} minute{'s' if mins != 1 else ''}**."
        await interaction.response.send_message(msg, ephemeral=True)


class SettingsView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id

        mods_only           = get_mods_only(guild_id)
        one_claim_per_batch = get_one_claim_per_batch(guild_id)
        claim_verification  = get_claim_verification(guild_id)
        verification_mode   = get_verification_mode(guild_id)
        dot_start_seconds   = get_dot_start_seconds(guild_id)
        cooldown_minutes    = get_claim_cooldown_minutes(guild_id)

        # Row 0 — toggle buttons
        mods_toggle = discord.ui.Button(
            label="Mods Only: ON" if mods_only else "Mods Only: OFF",
            style=discord.ButtonStyle.success if mods_only else discord.ButtonStyle.danger,
            custom_id="codeclaimer_toggle_mods_only",
            row=0,
        )
        mods_toggle.callback = self._toggle_mods_callback
        self.add_item(mods_toggle)

        batch_toggle = discord.ui.Button(
            label="One Claim Per Batch: ON" if one_claim_per_batch else "One Claim Per Batch: OFF",
            style=discord.ButtonStyle.success if one_claim_per_batch else discord.ButtonStyle.danger,
            custom_id="codeclaimer_toggle_one_claim_per_batch",
            row=0,
        )
        batch_toggle.callback = self._toggle_batch_callback
        self.add_item(batch_toggle)

        verify_toggle = discord.ui.Button(
            label="Claim Verification: ON" if claim_verification else "Claim Verification: OFF",
            style=discord.ButtonStyle.success if claim_verification else discord.ButtonStyle.danger,
            custom_id="codeclaimer_toggle_claim_verification",
            row=0,
        )
        verify_toggle.callback = self._toggle_verify_callback
        self.add_item(verify_toggle)

        # Row 1 — Challenge mode dropdown
        mode_select = discord.ui.Select(
            placeholder=f"Challenge Mode: {VERIFICATION_MODES.get(verification_mode, verification_mode)}",
            custom_id="codeclaimer_verification_mode_select",
            row=1,
            options=[
                discord.SelectOption(
                    label="Easy",
                    value="easy",
                    description="Simple addition or subtraction — answer under 10",
                    default=(verification_mode == "easy"),
                ),
                discord.SelectOption(
                    label="Medium",
                    value="medium",
                    description="PEMDAS expression — answer under 100",
                    default=(verification_mode == "medium"),
                ),
                discord.SelectOption(
                    label="Hard",
                    value="hard",
                    description="Algebraic equation — solve for x",
                    default=(verification_mode == "hard"),
                ),
                discord.SelectOption(
                    label="Difficulty Over Time",
                    value="difficulty_over_time",
                    description="Starts Hard, descends to Easy as time passes",
                    default=(verification_mode == "difficulty_over_time"),
                ),
                discord.SelectOption(
                    label="Periodic Table",
                    value="periodic",
                    description="Identify the symbol for a named element",
                    default=(verification_mode == "periodic"),
                ),
                discord.SelectOption(
                    label="Random",
                    value="random",
                    description="Bot picks a mode randomly for each claim",
                    default=(verification_mode == "random"),
                ),
            ],
        )
        mode_select.callback = self._mode_select_callback
        self.add_item(mode_select)

        # Row 2 — Claim role gate (multi-select)
        role_select = discord.ui.RoleSelect(
            placeholder="Claim Roles: Open to all — select to restrict",
            custom_id="codeclaimer_claim_roles_select",
            min_values=0,
            max_values=25,
            row=2,
        )
        role_select.callback = self._role_select_callback
        self.add_item(role_select)

        # Row 3 — Cooldown button
        cooldown_label = (
            f"Claim Cooldown: {cooldown_minutes}m"
            if cooldown_minutes > 0
            else "Claim Cooldown: Off"
        )
        cooldown_btn = discord.ui.Button(
            label=cooldown_label,
            style=discord.ButtonStyle.secondary,
            custom_id="codeclaimer_cooldown_btn",
            row=3,
        )
        cooldown_btn.callback = self._cooldown_callback
        self.add_item(cooldown_btn)

        # Row 4 — DOT timer config button (only when DOT mode is active)
        if verification_mode == "difficulty_over_time":
            dot_btn = discord.ui.Button(
                label=f"DOT Start Time: {dot_start_seconds}s",
                style=discord.ButtonStyle.secondary,
                custom_id="codeclaimer_dot_timer_btn",
                row=4,
            )
            dot_btn.callback = self._dot_timer_callback
            self.add_item(dot_btn)

    async def _toggle_mods_callback(self, interaction: discord.Interaction):
        if not await self._mod_check(interaction):
            return
        set_mods_only(interaction.guild.id, not get_mods_only(interaction.guild.id))
        await interaction.response.edit_message(
            embed=build_settings_embed(interaction.guild.id),
            view=SettingsView(interaction.guild.id),
        )

    async def _toggle_batch_callback(self, interaction: discord.Interaction):
        if not await self._mod_check(interaction):
            return
        set_one_claim_per_batch(interaction.guild.id, not get_one_claim_per_batch(interaction.guild.id))
        await interaction.response.edit_message(
            embed=build_settings_embed(interaction.guild.id),
            view=SettingsView(interaction.guild.id),
        )

    async def _toggle_verify_callback(self, interaction: discord.Interaction):
        if not await self._mod_check(interaction):
            return
        set_claim_verification(interaction.guild.id, not get_claim_verification(interaction.guild.id))
        await interaction.response.edit_message(
            embed=build_settings_embed(interaction.guild.id),
            view=SettingsView(interaction.guild.id),
        )

    async def _mode_select_callback(self, interaction: discord.Interaction):
        if not await self._mod_check(interaction):
            return
        selected = interaction.data["values"][0]
        set_verification_mode(interaction.guild.id, selected)
        await interaction.response.edit_message(
            embed=build_settings_embed(interaction.guild.id),
            view=SettingsView(interaction.guild.id),
        )

    async def _role_select_callback(self, interaction: discord.Interaction):
        if not await self._mod_check(interaction):
            return
        selected_ids = [int(v) for v in interaction.data.get("values", [])]
        set_claim_roles(interaction.guild.id, selected_ids)
        await interaction.response.edit_message(
            embed=build_settings_embed(interaction.guild.id),
            view=SettingsView(interaction.guild.id),
        )

    async def _cooldown_callback(self, interaction: discord.Interaction):
        if not await self._mod_check(interaction):
            return
        await interaction.response.send_modal(CooldownModal(interaction.guild.id))

    async def _dot_timer_callback(self, interaction: discord.Interaction):
        if not await self._mod_check(interaction):
            return
        await interaction.response.send_modal(DOTTimerModal(interaction.guild.id))

    async def _mod_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Settings can only be changed inside a server.", ephemeral=True
            )
            return False
        if not is_moderator(interaction.user):
            await interaction.response.send_message(
                "Only moderators can change CodeClaimer settings.", ephemeral=True
            )
            return False
        return True


class BulkShareModal(discord.ui.Modal, title="Bulk Share Codes"):
    pre_drop_message = discord.ui.TextInput(
        label="Optional message to post before the drop",
        style=discord.TextStyle.short,
        required=False,
        max_length=500,
        placeholder="e.g. 🎮 Game drop from @Username! One per person!",
    )

    batch_data = discord.ui.TextInput(
        label="Paste codes, one per line",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000,
        placeholder=(
            "Game (Steam): ABC-123 | 12/31/2026\n"
            "Game 2: DEF-456"
        ),
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not await user_can_use_bot(interaction):
            await interaction.followup.send(
                "CodeClaimer is currently set to **Mods Only: ON**. "
                "Ask a moderator to share these codes.",
                ephemeral=True,
            )
            return

        raw_text    = str(self.batch_data.value)
        pre_message = str(self.pre_drop_message.value).strip() if self.pre_drop_message.value else ""

        if contains_link(raw_text):
            await interaction.followup.send(
                "❌ **Submission Rejected:** Links and web addresses are not allowed.",
                ephemeral=True,
            )
            return

        valid_entries, skipped_entries = [], []

        for item in split_bulk_entries(raw_text):
            parsed = parse_bulk_entry(item)
            if parsed is None:
                skipped_entries.append(item)
                continue

            item_name, platform, product_code, expires_at = parsed

            if expires_at and not expiration_is_valid(expires_at):
                skipped_entries.append(f"{item}  [Invalid expiration — use MM/DD/YYYY]")
                continue
            if is_expired(expires_at):
                skipped_entries.append(f"{item}  [Already expired]")
                continue

            valid_entries.append((item_name, platform, product_code, expires_at))

        if not valid_entries:
            await interaction.followup.send(
                (
                    "❌ **Format Error:** No valid entries found.\n\n"
                    "Use one code per line:\n"
                    "`Product Name (Platform): Code | Optional Expiration`\n\n"
                    "Examples:\n"
                    "`Hollow Knight (Steam): ABC-123 | 12/31/2026`\n"
                    "`Celeste: DEF-456`"
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Processing **{len(valid_entries)}** entries...", ephemeral=True
        )

        # Post the optional pre-drop message to the channel first
        if pre_message:
            try:
                await interaction.channel.send(pre_message)
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(f"[BulkShare] Could not post pre-drop message: {exc}")

        # All cards in this modal submission share one batch_id
        batch_id = str(uuid.uuid4())

        for item_name, platform, product_code, expires_at in valid_entries:
            await post_claim_card(
                channel=interaction.channel,
                sharer=interaction.user,
                item_name=item_name,
                platform=platform,
                product_code=product_code,
                expires_at=expires_at,
                batch_id=batch_id,
            )
            await asyncio.sleep(0.6)

        if skipped_entries:
            preview  = "\n".join(f"- {e}" for e in skipped_entries[:5])
            overflow = f"\n...and {len(skipped_entries) - 5} more." if len(skipped_entries) > 5 else ""
            await interaction.followup.send(
                f"Done. Posted **{len(valid_entries)}** entries.\n\n"
                f"Skipped **{len(skipped_entries)}**:\n{preview}{overflow}",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Done. Posted **{len(valid_entries)}** entries.", ephemeral=True
            )


class BulkSharePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Open Bulk Entry Form",
        style=discord.ButtonStyle.primary,
        custom_id="codeclaimer_open_bulk_modal",
    )
    async def open_bulk_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await user_can_use_bot(interaction):
            await interaction.response.send_message(
                "CodeClaimer is currently set to **Mods Only: ON**. "
                "Ask a moderator to share these codes.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(BulkShareModal())


# ---------------------------------------------------------------------------
# Settings embed builder
# ---------------------------------------------------------------------------

def build_settings_embed(guild_id: int) -> discord.Embed:
    mods_only           = get_mods_only(guild_id)
    one_claim_per_batch = get_one_claim_per_batch(guild_id)
    claim_verification  = get_claim_verification(guild_id)
    verification_mode   = get_verification_mode(guild_id)
    dot_start_seconds   = get_dot_start_seconds(guild_id)
    claim_roles         = get_claim_roles(guild_id)
    cooldown_minutes    = get_claim_cooldown_minutes(guild_id)

    embed = discord.Embed(
        title="CodeClaimer Settings",
        description="Manage how CodeClaimer works in this server.",
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="Access",
        value=(
            f"**Mods Only: {'ON' if mods_only else 'OFF'}**\n"
            + (
                "Only moderators can use `/sharecode` and `/bulkshare`."
                if mods_only
                else "All members can use `/sharecode` and `/bulkshare`."
            )
            + "\n\nModerator = Administrator, Manage Server, or Manage Messages."
        ),
        inline=False,
    )
    embed.add_field(
        name="One Claim Per Batch",
        value=(
            f"**{'ON' if one_claim_per_batch else 'OFF'}**\n"
            + (
                "Each member can claim only one code per `/bulkshare` drop."
                if one_claim_per_batch
                else "Members can claim multiple codes from the same `/bulkshare` drop."
            )
            + "\n\nThis setting only applies to `/bulkshare`. "
            "Single `/sharecode` drops are not affected."
        ),
        inline=False,
    )

    mode_label = VERIFICATION_MODES.get(verification_mode, verification_mode)
    if verification_mode == "difficulty_over_time":
        durations = _dot_tier_durations(dot_start_seconds)
        tier_str = " → ".join(
            f"{int(d)}s" if d is not None else "Easy (holds)"
            for d in durations
        )
        mode_detail = f"\n*Timer: {tier_str}*"
    else:
        mode_detail = ""

    embed.add_field(
        name="Claim Verification",
        value=(
            f"**{'ON' if claim_verification else 'OFF'}** — Mode: **{mode_label}**{mode_detail}\n"
            + (
                "Members must solve a challenge before receiving a code."
                if claim_verification
                else "No verification required. First to click wins."
            )
            + "\n\nHelps slow fast claiming and reduces automated claiming."
        ),
        inline=False,
    )

    if claim_roles:
        role_mentions = " ".join(f"<@&{rid}>" for rid in claim_roles)
        role_value = (
            f"**Restricted** — {role_mentions}\n"
            "Members need at least one of these roles to claim. Mods always bypass."
        )
    else:
        role_value = "**Open to all** — No role restriction on claiming."

    embed.add_field(
        name="Claim Role Gate",
        value=role_value,
        inline=False,
    )

    if cooldown_minutes > 0:
        cooldown_value = (
            f"**{cooldown_minutes} minute{'s' if cooldown_minutes != 1 else ''}** — "
            "Members must wait this long between claims in this server."
        )
    else:
        cooldown_value = "**Off** — No cooldown between claims."

    embed.add_field(
        name="Claim Cooldown",
        value=cooldown_value,
        inline=False,
    )

    embed.add_field(
        name="Support",
        value=(
            "[Vote on Top.gg](https://top.gg/bot/1511758084194832495)"
            " | "
            "[Send some Ko-fi](https://ko-fi.com/artchemylabs)"
            " | "
            "[Support Server](https://discord.gg/NSt5Rcm8VN)"
        ),
        inline=False,
    )
    return embed


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

class CodeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(ClaimButtonView())
        self.add_view(BulkSharePanelView())


bot = CodeBot()


# ---------------------------------------------------------------------------
# Expiration background task
# ---------------------------------------------------------------------------

@tasks.loop(minutes=10)
async def expire_unclaimed_codes():
    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT message_id, item_name, platform, expires_at, channel_id
                FROM shared_codes
                WHERE expires_at IS NOT NULL AND expires_at != ''
                """
            ).fetchall()

        if not rows:
            return

        expired = [r for r in rows if is_expired(r["expires_at"])]
        if not expired:
            return

        print(f"[Expiration] Processing {len(expired)} expired code(s).")

        for row in expired:
            await mark_code_expired(
                message_id=row["message_id"],
                item_name=row["item_name"],
                platform=row["platform"],
                expires_at=row["expires_at"],
                channel_id=row["channel_id"],
            )
            await asyncio.sleep(0.4)

    except Exception as exc:
        print(f"[Expiration] Unhandled error in expiration task: {exc}")


@expire_unclaimed_codes.before_loop
async def before_expire():
    await bot.wait_until_ready()


TOPGG_TOKEN = os.getenv("TOPGG_TOKEN")

@tasks.loop(minutes=30)
async def post_topgg_stats():
    if not TOPGG_TOKEN:
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"https://top.gg/api/bots/{bot.user.id}/stats",
                json={"server_count": len(bot.guilds)},
                headers={"Authorization": TOPGG_TOKEN},
            )
        print(f"[Top.gg] Posted server count: {len(bot.guilds)}")
    except Exception as exc:
        print(f"[Top.gg] Failed to post stats: {exc}")


@post_topgg_stats.before_loop
async def before_topgg():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user.name}")
    print(f"Database: {DB_PATH}")

    seed_guild_settings([g.id for g in bot.guilds])
    print(f"Guild settings seeded for {len(bot.guilds)} server(s).")

    if not expire_unclaimed_codes.is_running():
        expire_unclaimed_codes.start()
        print("Expiration checker started.")

    if TOPGG_TOKEN and not post_topgg_stats.is_running():
        post_topgg_stats.start()
        print("Top.gg stats poster started.")

    try:
        await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as exc:
        print(f"Failed to sync commands: {exc}")


@bot.event
async def on_guild_join(guild: discord.Guild):
    seed_guild_settings([guild.id])
    print(f"Joined {guild.name} ({guild.id}) — settings seeded.")


@bot.event
async def on_disconnect():
    print("[Connection] Bot disconnected from Discord.")


@bot.event
async def on_resumed():
    print("[Connection] Bot connection resumed.")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="sharecode", description="Share a spare product code with the community safely.")
@app_commands.describe(
    item_name="Name of the game or product",
    code="Secret activation code",
    platform="Optional platform, like Steam, Epic, PS5, or Xbox",
    expires_at="Optional expiration date, like 12/31/2026",
)
async def sharecode(
    interaction: discord.Interaction,
    item_name: str,
    code: str,
    platform: str = "",
    expires_at: str = "",
):
    await interaction.response.defer(ephemeral=True)

    if not await user_can_use_bot(interaction):
        await interaction.followup.send(
            "CodeClaimer is set to **Mods Only: ON**. Ask a moderator to share this code.",
            ephemeral=True,
        )
        return

    platform   = clean_platform(platform)
    expires_at = clean_expiration(expires_at)

    if expires_at and not expiration_is_valid(expires_at):
        await interaction.followup.send(
            "❌ **Expiration Error:** Use `MM/DD/YYYY`, e.g. `12/31/2026`.",
            ephemeral=True,
        )
        return

    if contains_link(code) or contains_link(item_name) or contains_link(platform) or contains_link(expires_at):
        await interaction.followup.send(
            "❌ **Submission Rejected:** Links and web addresses are not allowed.",
            ephemeral=True,
        )
        return

    if is_expired(expires_at):
        await interaction.followup.send(
            "❌ **Expiration Error:** That date has already passed.",
            ephemeral=True,
        )
        return

    await interaction.followup.send("Your code has been posted publicly.", ephemeral=True)
    await post_claim_card(
        channel=interaction.channel,
        sharer=interaction.user,
        item_name=item_name,
        platform=platform,
        product_code=code,
        expires_at=expires_at,
        batch_id="",
    )


@bot.tree.command(name="bulkshare", description="Open a guided panel to share multiple codes at once.")
async def bulkshare(interaction: discord.Interaction):
    if not await user_can_use_bot(interaction):
        await interaction.response.send_message(
            "CodeClaimer is set to **Mods Only: ON**. Ask a moderator to share these codes.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="Bulk Share Codes",
        description=(
            "Click **Open Bulk Entry Form** and paste your codes — one per line.\n\n"
            "**Format:**\n"
            "`Product Name (Platform): Code | MM/DD/YYYY`\n\n"
            "Platform and expiration are optional:\n"
            "`Product Name: Code`\n\n"
            "**Examples:**\n"
            "`Hollow Knight (Steam): ABC-123 | 12/31/2026`\n"
            "`Celeste: DEF-456`\n"
            "`Minecraft Skin Pack (Xbox): GHI-789 | 10/01/2026`\n\n"
            "You can also include an optional message to post above the drop."
        ),
        color=discord.Color.blue(),
    )
    await interaction.response.send_message(
        embed=embed, view=BulkSharePanelView(), ephemeral=True
    )


@bot.tree.command(name="settings", description="Open CodeClaimer settings.")
async def settings_command(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Settings can only be opened inside a server.", ephemeral=True
        )
        return
    if not is_moderator(interaction.user):
        await interaction.response.send_message(
            "Only moderators can access CodeClaimer settings.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        embed=build_settings_embed(interaction.guild.id),
        view=SettingsView(interaction.guild.id),
        ephemeral=True,
    )


@bot.tree.command(name="help", description="Show CodeClaimer usage instructions.")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="CodeClaimer Help",
        description=(
            "CodeClaimer lets your community safely share spare product, game, or access codes. "
            "Codes are hidden publicly and sent by DM to the first person who claims them."
        ),
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="/sharecode",
        value=(
            "`item_name` — Name of the game or product\n"
            "`code` — The private code\n"
            "`platform` — Optional, e.g. Steam, Epic, PS5, Xbox\n"
            "`expires_at` — Optional expiration date, e.g. `12/31/2026`\n\n"
            "**With expiration:**\n"
            "`/sharecode item_name: Hollow Knight code: ABC-123 platform: Steam expires_at: 12/31/2026`\n\n"
            "**Without:**\n"
            "`/sharecode item_name: Celeste code: DEF-456`"
        ),
        inline=False,
    )
    embed.add_field(
        name="/bulkshare",
        value=(
            "Opens a guided panel. Click **Open Bulk Entry Form** and paste one code per line.\n\n"
            "`Product Name (Platform): Code | MM/DD/YYYY`\n\n"
            "You can include an optional message that posts above the drop — "
            "useful for tagging roles or crediting a sharer.\n\n"
            "If **One Claim Per Batch** is ON in `/settings`, each member can only claim "
            "one code per bulk drop. This does not apply to `/sharecode`.\n\n"
            "If **Claim Verification** is ON, members must answer a quick math question "
            "before receiving their code."
        ),
        inline=False,
    )
    embed.add_field(
        name="/settings",
        value=(
            "Toggle server settings:\n"
            "**Mods Only** — restrict sharing commands to moderators\n"
            "**One Claim Per Batch** — one code per member per bulk drop\n"
            "**Claim Verification** — challenge question before claiming\n\n"
            "**Challenge Modes** (dropdown in /settings):\n"
            "Easy — simple addition or subtraction\n"
            "Medium — PEMDAS expression, answer under 100\n"
            "Hard — algebraic equation, solve for x\n"
            "Difficulty Over Time — starts Hard, descends to Easy as time passes\n"
            "Periodic Table — identify the symbol for a named element\n"
            "Random — bot picks a mode fresh each claim\n\n"
            "Moderator = Administrator, Manage Server, or Manage Messages."
        ),
        inline=False,
    )
    embed.add_field(
        name="Rules",
        value=(
            "No links or web addresses allowed in any field. "
            "The first person to answer correctly (if verification is ON) or click "
            "claims the code by DM. "
            "Claimed and expired cards are greyed out with the button removed."
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with bot:
        await bot.start(BOT_TOKEN)


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("Bot stopped.")
