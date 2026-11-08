"""
resurface.py — Bring buried codes back, plus last call, raffle reminders and
the 60 day cleanup.

Control model
  * Everything is off until a mod picks channels in /settings > Resurfacing.
    Servers where anyone can share still need a mod to turn this on.
  * A roundup only lists codes posted in its own channel. Codes never move to
    another channel, so a code shared in a members only channel never shows up
    in a public one.
  * Roundups show item names, platforms and expiry only. Never codes, never
    sharer names. Claiming from a roundup or from Browse all runs the same
    challenge, role gate and cooldown as the card.
  * Every control is mod only: settings, the preview, Approve and Skip, cleanup.

Triggers (checked every minute per enabled channel)
  activity  N new messages since the last bot post AND at least T since the
            last roundup AND under the daily cap
  schedule  the period has passed AND someone posted since the last roundup
  Roundups covering more than 25 codes wait for a mod's Approve in the mod
  updates channel when one is set.

Each code is featured in at most 5 roundups. Featured codes rotate because the
count rises each time; Browse all always lists every open code.
"""
import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

import discord
from discord import ui
from discord.ext import tasks

import cards
import db
from parsing import expiry_timestamp, is_expired

log = logging.getLogger("codeclaimer.resurface")

PRESETS = {  # name: (messages, min gap seconds, daily cap)
    "light":    (50, 6 * 3600, 3),
    "standard": (25, 2 * 3600, 6),
    "busy":     (15, 30 * 60, 12),
}
PRESET_LABELS = {
    "off": "Off",
    "light": "Light · after 50 messages, at most every 6 hours, 3 a day",
    "standard": "Standard · after 25 messages, at most every 2 hours, 6 a day",
    "busy": "Busy · after 15 messages, at most every 30 minutes, 12 a day",
}
SCHEDULES = {"48h": 48 * 3600, "7d": 7 * 86400, "14d": 14 * 86400, "30d": 30 * 86400}
SCHEDULE_LABELS = {"off": "Off", "48h": "Every 48 hours", "7d": "Every 7 days",
                   "14d": "Every 14 days", "30d": "Monthly"}

APPROVAL_THRESHOLD = 25
APPROVAL_HOURS = 2
MAX_FEATURES = 5
TEASER = 5
PAGE = 10
CLEANUP_DAYS = 60

PICK_ID = "cc:pick"
BROWSE_PREFIX = "cc:browse:"
CLAIMREF_PREFIX = "cc:claim:"
APPROVE_PREFIX = "cc:approve:"
SKIP_PREFIX = "cc:skip:"

_bot = None
_counts: dict[int, int] = {}       # channel_id -> messages since the last bot post
_enabled: set[int] = set()         # channels with resurfacing on
_dirty: set[int] = set()           # channels whose roundup should be redrawn


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def init_tables() -> None:
    with db.get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS resurface_state (
                channel_id      INTEGER PRIMARY KEY,
                guild_id        INTEGER NOT NULL,
                last_roundup_at TEXT,
                roundup_msg     INTEGER,
                day             TEXT,
                today           INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS resurface_approvals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id     INTEGER NOT NULL,
                channel_id   INTEGER NOT NULL,
                card_channel INTEGER,
                card_msg     INTEGER,
                trigger      TEXT,
                codes        INTEGER,
                created_at   TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending'
            )
        """)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(raffles)")}
        if cols and "reminder_msg" not in cols:
            conn.execute("ALTER TABLE raffles ADD COLUMN reminder_msg INTEGER")
        conn.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _state(channel_id: int) -> dict:
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM resurface_state WHERE channel_id = ?", (channel_id,)).fetchone()
    return dict(row) if row else {}


def _save_state(channel_id: int, guild_id: int, **fields) -> None:
    with db.get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO resurface_state (channel_id, guild_id) VALUES (?, ?)",
                     (channel_id, guild_id))
        for k, v in fields.items():
            conn.execute(f"UPDATE resurface_state SET {k} = ? WHERE channel_id = ?", (v, channel_id))
        conn.commit()


def arm_channels(guild_id: int, channel_ids) -> None:
    """Start the clock for newly enabled channels so nothing fires the instant they're picked."""
    for ch in channel_ids:
        if not _state(ch).get("last_roundup_at"):
            _save_state(ch, guild_id, last_roundup_at=_now().isoformat())
        _counts.setdefault(ch, 0)


def posted_at(row: dict) -> datetime:
    return discord.utils.snowflake_time(row["message_id"])


def open_codes(channel_id: int) -> list[dict]:
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM shared_codes WHERE channel_id = ? AND product_code != ''", (channel_id,)).fetchall()]
    return [r for r in rows if not is_expired(r.get("expires_at"))]


def _sort_key(r: dict, now_ts: float):
    exp = expiry_timestamp(r.get("expires_at"))
    soon = exp is not None and exp - now_ts <= 7 * 86400
    return (0 if soon else 1, exp if soon else 0, r.get("resurface_count") or 0, r["message_id"])


def featured(rows: list[dict]) -> list[dict]:
    now_ts = _now().timestamp()
    eligible = [r for r in rows if (r.get("resurface_count") or 0) < MAX_FEATURES]
    return sorted(eligible, key=lambda r: _sort_key(r, now_ts))[:TEASER]


def effective_channels(s: dict) -> set[int]:
    if s["resurface_activity"] == "off" and s["resurface_schedule"] == "off":
        return set()
    return set(s["resurface_channels"])


def refresh_enabled() -> None:
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT guild_id FROM guild_settings
               WHERE resurface_channels != '' AND (resurface_activity != 'off' OR resurface_schedule != 'off'
                     OR last_call = 1 OR raffle_reminder = 1)""").fetchall()
    chans = set()
    for (gid,) in rows:
        chans |= set(db.get_settings(gid)["resurface_channels"])
    _enabled.clear()
    _enabled.update(chans)


# ---------------------------------------------------------------------------
# Message counting
# ---------------------------------------------------------------------------

def on_message(message: discord.Message) -> None:
    if message.guild is None or message.channel.id not in _enabled:
        return
    if _bot and _bot.user and message.author.id == _bot.user.id:
        _counts[message.channel.id] = 0
    else:
        _counts[message.channel.id] = _counts.get(message.channel.id, 0) + 1


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _when_line(r: dict) -> str:
    ts = expiry_timestamp(r.get("expires_at"))
    if ts:
        return f"expires <t:{ts}:R>"
    return f"posted <t:{int(posted_at(r).timestamp())}:d>"


def _when_plain(r: dict) -> str:
    ts = expiry_timestamp(r.get("expires_at"))
    if ts:
        left = ts - _now().timestamp()
        if left < 3600:
            return "expires within the hour"
        if left < 86400:
            return f"expires in {int(left // 3600)} hours"
        return f"expires in {int(left // 86400)} days"
    return "posted " + posted_at(r).strftime("%b %d").replace(" 0", " ")


def _line(r: dict, n: int | None = None) -> str:
    plat = f"  ·  {r['platform']}" if r.get("platform") else ""
    num = f"`{n}.` " if n is not None else ""
    return f"{num}**{r['item_name']}**{plat}  ·  {_when_line(r)}"


def _option(r: dict) -> discord.SelectOption:
    desc = "  ·  ".join(x for x in (r.get("platform") or "", _when_plain(r)) if x)
    return discord.SelectOption(label=r["item_name"][:100], value=str(r["message_id"]), description=desc[:100])


# ---------------------------------------------------------------------------
# Roundup card
# ---------------------------------------------------------------------------

def roundup_view(channel_id: int, rows: list[dict], feat: list[dict]) -> ui.LayoutView:
    n = len(rows)
    title = "## A code is waiting for a claimer" if n == 1 else "## Codes waiting for a claimer"
    parts = [*cards.tile_section([f"-# STILL UP FOR GRABS  ·  {n} OPEN IN THIS CHANNEL", title],
                                 cards.TILE_LIVE, "Claw reaching for a gift"),
             ui.TextDisplay("\n".join(_line(r) for r in feat))]
    if n > len(feat):
        parts.append(ui.TextDisplay(f"-# and {n - len(feat)} more. A different set shows each time."))
    parts.append(ui.Separator())
    pick = ui.Select(custom_id=PICK_ID, placeholder=f"Pick one of these {len(feat)} to claim",
                     options=[_option(r) for r in feat])
    parts.append(ui.ActionRow(pick))
    if n > len(feat):
        parts.append(ui.ActionRow(ui.Button(label=f"Browse all {n}", style=discord.ButtonStyle.primary,
                                            custom_id=f"{BROWSE_PREFIX}{channel_id}")))
    parts.append(ui.TextDisplay(cards.INVITE_LINE))
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_color=cards.SKY))
    return view


async def _channel(channel_id: int):
    ch = _bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await _bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return ch


async def post_roundup(guild_id: int, channel_id: int, trigger: str) -> bool:
    rows = open_codes(channel_id)
    feat = featured(rows)
    if not feat:
        return False
    ch = await _channel(channel_id)
    if ch is None:
        log.info("Roundup skipped channel=%d: channel not reachable", channel_id)
        return False
    try:
        msg = await ch.send(view=roundup_view(channel_id, rows, feat),
                            allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        log.info("Roundup post failed channel=%d: %s", channel_id, e)
        return False

    st = _state(channel_id)
    old = st.get("roundup_msg")
    today = _now().strftime("%Y-%m-%d")
    count = (st.get("today") or 0) + 1 if st.get("day") == today else 1
    _save_state(channel_id, guild_id, last_roundup_at=_now().isoformat(), roundup_msg=msg.id, day=today,
                today=count)
    with db.get_db() as conn:
        conn.executemany("UPDATE shared_codes SET resurface_count = resurface_count + 1 WHERE message_id = ?",
                         [(r["message_id"],) for r in feat])
        conn.commit()
    _counts[channel_id] = 0
    if old:
        try:
            await ch.get_partial_message(old).delete()
        except discord.HTTPException:
            pass
    log.info("Roundup posted guild=%d channel=%d open=%d featured=%d trigger=%s",
             guild_id, channel_id, len(rows), len(feat), trigger)
    return True


async def _redraw_roundup(channel_id: int) -> None:
    st = _state(channel_id)
    if not st.get("roundup_msg"):
        return
    ch = await _channel(channel_id)
    if ch is None:
        return
    msg = ch.get_partial_message(st["roundup_msg"])
    rows = open_codes(channel_id)
    keep = featured(rows)
    try:
        if not rows:
            await msg.delete()
            _save_state(channel_id, st["guild_id"], roundup_msg=None)
        elif keep:
            await msg.edit(view=roundup_view(channel_id, rows, keep), allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------------
# Claiming from a roundup, Browse all, last call buttons
# ---------------------------------------------------------------------------

async def handle_pick(interaction: discord.Interaction) -> None:
    import claims
    values = (interaction.data or {}).get("values") or []
    if not values:
        await interaction.response.defer()
        return
    await claims.handle_claim_click(interaction, message_id=int(values[0]))


async def handle_claim_ref(interaction: discord.Interaction, suffix: str) -> None:
    import claims
    await claims.handle_claim_click(interaction, message_id=int(suffix))


class BrowseView(ui.LayoutView):
    def __init__(self, channel_id: int, page: int = 0):
        super().__init__(timeout=900)
        self.channel_id = channel_id
        rows = open_codes(channel_id)
        rows.sort(key=lambda r: _sort_key(r, _now().timestamp())[:2] + (r["message_id"],))
        pages = max(1, math.ceil(len(rows) / PAGE))
        self.page = page = min(max(0, page), pages - 1)
        chunk = rows[page * PAGE:(page + 1) * PAGE]
        parts: list = [ui.TextDisplay(f"-# OPEN CODES IN <#{channel_id}>  ·  PAGE {page + 1} OF {pages}")]
        if not chunk:
            parts.append(ui.TextDisplay("Every code here has been claimed. Keep an eye out for the next drop!"))
        else:
            parts.append(ui.TextDisplay("\n".join(_line(r, page * PAGE + i + 1) for i, r in enumerate(chunk))))
            parts.append(ui.Separator())
            pick = ui.Select(placeholder="Pick a code on this page to claim", options=[_option(r) for r in chunk])
            pick.callback = self._pick
            parts.append(ui.ActionRow(pick))
        prev = ui.Button(label="Prev", style=discord.ButtonStyle.secondary, disabled=page == 0)
        nxt = ui.Button(label="Next", style=discord.ButtonStyle.secondary, disabled=page >= pages - 1)
        prev.callback, nxt.callback = self._prev, self._next
        parts.append(ui.ActionRow(prev, ui.Button(label=f"{page + 1} / {pages}", disabled=True,
                                                  style=discord.ButtonStyle.secondary), nxt))
        self.add_item(ui.Container(*parts, accent_color=cards.SKY))

    async def _pick(self, interaction: discord.Interaction):
        import claims
        await claims.handle_claim_click(interaction, message_id=int(interaction.data["values"][0]))

    async def _prev(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=BrowseView(self.channel_id, self.page - 1))

    async def _next(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=BrowseView(self.channel_id, self.page + 1))


async def handle_browse(interaction: discord.Interaction, suffix: str) -> None:
    from claims import is_moderator
    channel_id = int(suffix)
    if interaction.guild_id:
        roles = db.get_settings(interaction.guild_id)["claim_roles"]
        if roles and not is_moderator(interaction.user):
            if not {r.id for r in getattr(interaction.user, "roles", [])} & set(roles):
                mentions = " ".join(f"<@&{r}>" for r in roles[:5])
                await interaction.response.send_message(f"Claiming here needs one of these roles: {mentions}",
                                                        ephemeral=True)
                return
    await interaction.response.send_message(view=BrowseView(channel_id), ephemeral=True)


# ---------------------------------------------------------------------------
# When a code leaves (claimed, expired, withdrawn, deleted, cleared)
# ---------------------------------------------------------------------------

def _closed_ref_view(row: dict, label: str, line: str, tile: str, accent: int) -> ui.LayoutView:
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(
        *cards.tile_section([f"-# {label}", f"### {cards.item_label(row['item_name'], row.get('platform'))}\n{line}"],
                            tile, label.title()),
        accent_color=accent,
    ))
    return view


async def on_code_gone(row: dict, state: str, claimer: str | None = None) -> None:
    """Hook for claims, expiry, withdraw, delete and cleanup. Never raises."""
    try:
        await _code_gone(row, state, claimer)
    except Exception:
        log.exception("Resurface hook failed msg=%s", row.get("message_id"))


async def _code_gone(row: dict, state: str, claimer: str | None) -> None:
    if row.get("channel_id"):
        _dirty.add(row["channel_id"])
    lc = row.get("last_call_msg")
    if not lc or not _bot:
        return
    if state == "claimed":
        view = _closed_ref_view(row, "CLAIMED", f"Claimed by **{claimer or 'someone'}**.", cards.TILE_CLAIMED,
                                cards.NAVY)
    else:
        view = _closed_ref_view(row, state.upper(), "No longer up for grabs.", cards.TILE_EMPTY, cards.SLATE)
    ch = await _channel(row["channel_id"])
    if ch is None:
        return
    try:
        await ch.get_partial_message(lc).edit(view=view, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------------
# Last call and raffle closing soon
# ---------------------------------------------------------------------------

def last_call_view(row: dict, ts: int) -> ui.LayoutView:
    sharer = f"<@{row['sharer_id']}>" if row.get("sharer_id") else (row.get("sharer_name") or "someone")
    plat = f"**{row['platform']}**  ·  " if row.get("platform") else ""
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(
        *cards.tile_section([f"-# LAST CALL  ·  EXPIRES <t:{ts}:R>", f"## {row['item_name']}\n{plat}Shared by {sharer}"],
                            cards.TILE_LIVE, "Claw reaching for a gift"),
        ui.Separator(),
        ui.ActionRow(ui.Button(label="Claim code", style=discord.ButtonStyle.success, emoji="🎁",
                               custom_id=f"{CLAIMREF_PREFIX}{row['message_id']}")),
        ui.TextDisplay(cards.INVITE_LINE),
        accent_color=cards.SKY,
    ))
    return view


async def _last_calls(guild_id: int, s: dict) -> None:
    now_ts = _now().timestamp()
    for ch_id in s["resurface_channels"]:
        for row in open_codes(ch_id):
            if row.get("last_call_msg"):
                continue
            ts = expiry_timestamp(row.get("expires_at"))
            if not ts:
                continue
            lifetime = ts - posted_at(row).timestamp()
            window = 3600 if lifetime <= 86400 else 86400
            if ts - now_ts > window:
                continue
            ch = await _channel(ch_id)
            msg_id = 0
            if ch is not None:
                try:
                    msg = await ch.send(view=last_call_view(row, ts), allowed_mentions=discord.AllowedMentions.none())
                    msg_id = msg.id
                    log.info("Last call posted guild=%d channel=%d code_msg=%d", guild_id, ch_id, row["message_id"])
                except discord.HTTPException as e:
                    log.info("Last call failed channel=%d: %s", ch_id, e)
            with db.get_db() as conn:
                conn.execute("UPDATE shared_codes SET last_call_msg = ? WHERE message_id = ?",
                             (msg_id or -1, row["message_id"]))
                conn.commit()
            await asyncio.sleep(0.5)


def raffle_reminder_view(r: dict, entries: int, winners: int) -> ui.LayoutView:
    ends = int(datetime.fromisoformat(r["ends_at"]).timestamp())
    jump = f"https://discord.com/channels/{r['guild_id']}/{r['channel_id']}/{r['message_id']}"
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(
        *cards.tile_section([f"-# RAFFLE CLOSING SOON  ·  {winners} WINNER{'S' if winners != 1 else ''}",
                             f"## {r['title'] or 'Raffle'}\nDraw <t:{ends}:R>  ·  **{entries}** entered"],
                            cards.TILE_LIVE, "Claw reaching for a gift"),
        ui.Separator(),
        ui.ActionRow(
            ui.Button(label="Enter", style=discord.ButtonStyle.success, emoji="🎟️",
                      custom_id=f"cc:raffle:enter:{r['message_id']}"),
            ui.Button(label="Jump to raffle", style=discord.ButtonStyle.link, url=jump),
        ),
        accent_color=cards.SKY,
    ))
    return view


async def _raffle_reminders(guild_id: int, s: dict) -> None:
    import raffle
    soon = (_now() + timedelta(hours=1)).isoformat()
    chans = set(s["resurface_channels"])
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT * FROM raffles WHERE guild_id = ? AND status = 'open' AND ends_at <= ?
               AND reminder_msg IS NULL""", (guild_id, soon)).fetchall()]
    for r in rows:
        if r["channel_id"] not in chans:
            continue
        if datetime.fromisoformat(r["ends_at"]) - _now() < timedelta(minutes=5):
            msg_id = -1  # too close to the draw to be useful
        else:
            ch = await _channel(r["channel_id"])
            msg_id = -1
            if ch is not None:
                try:
                    msg = await ch.send(view=raffle_reminder_view(r, raffle.entry_count(r["message_id"]),
                                                                  len(raffle.raffle_codes(r["message_id"]))),
                                        allowed_mentions=discord.AllowedMentions.none())
                    msg_id = msg.id
                    log.info("Raffle reminder posted guild=%d raffle=%d", guild_id, r["message_id"])
                except discord.HTTPException as e:
                    log.info("Raffle reminder failed raffle=%d: %s", r["message_id"], e)
        with db.get_db() as conn:
            conn.execute("UPDATE raffles SET reminder_msg = ? WHERE message_id = ?", (msg_id, r["message_id"]))
            conn.commit()


async def on_raffle_closed(r: dict) -> None:
    """Remove the closing soon post once the raffle is drawn or cancelled. Never raises."""
    try:
        await _raffle_closed(r)
    except Exception:
        log.exception("Raffle reminder cleanup failed raffle=%s", r.get("message_id"))


async def _raffle_closed(r: dict) -> None:
    if _bot is None:
        return
    with db.get_db() as conn:
        row = conn.execute("SELECT reminder_msg FROM raffles WHERE message_id = ?", (r["message_id"],)).fetchone()
    if not row or not row[0] or row[0] < 0:
        return
    ch = await _channel(r["channel_id"])
    if ch is None:
        return
    try:
        await ch.get_partial_message(row[0]).delete()
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------------
# Mod approval for big roundups
# ---------------------------------------------------------------------------

def _approval_view(aid: int, channel_id: int, codes: int, trigger: str, done: str | None = None) -> ui.LayoutView:
    view = ui.LayoutView(timeout=None)
    why = {"activity": "chat activity", "schedule": "the schedule"}.get(trigger, trigger)
    parts = [ui.TextDisplay("-# APPROVAL NEEDED" if not done else "-# ROUNDUP REQUEST"),
             ui.TextDisplay(f"### Roundup for <#{channel_id}>\n{codes} open codes. Triggered by {why}.")]
    if done:
        parts.append(ui.TextDisplay(done))
    else:
        parts.append(ui.ActionRow(
            ui.Button(label="Approve", style=discord.ButtonStyle.success, custom_id=f"{APPROVE_PREFIX}{aid}"),
            ui.Button(label="Skip", style=discord.ButtonStyle.secondary, custom_id=f"{SKIP_PREFIX}{aid}"),
        ))
        parts.append(ui.TextDisplay(f"-# Mods only. Skipped on its own if nobody answers within {APPROVAL_HOURS} hours."))
    view.add_item(ui.Container(*parts, accent_color=cards.NAVY))
    return view


async def _request_approval(guild_id: int, channel_id: int, mod_channel_id: int, trigger: str, codes: int) -> None:
    with db.get_db() as conn:
        pending = conn.execute("SELECT 1 FROM resurface_approvals WHERE channel_id = ? AND status = 'pending'",
                               (channel_id,)).fetchone()
        if pending:
            return
        aid = conn.execute(
            """INSERT INTO resurface_approvals (guild_id, channel_id, card_channel, trigger, codes, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (guild_id, channel_id, mod_channel_id, trigger, codes, _now().isoformat())).lastrowid
        conn.commit()
    ch = await _channel(mod_channel_id)
    try:
        if ch is None:
            raise LookupError("mod updates channel not found")
        msg = await ch.send(view=_approval_view(aid, channel_id, codes, trigger),
                            allowed_mentions=discord.AllowedMentions.none())
        with db.get_db() as conn:
            conn.execute("UPDATE resurface_approvals SET card_msg = ? WHERE id = ?", (msg.id, aid))
            conn.commit()
        log.info("Roundup approval requested guild=%d channel=%d codes=%d", guild_id, channel_id, codes)
    except (discord.HTTPException, LookupError) as e:
        with db.get_db() as conn:
            conn.execute("UPDATE resurface_approvals SET status = 'failed' WHERE id = ?", (aid,))
            conn.commit()
        _save_state(channel_id, guild_id, last_roundup_at=_now().isoformat())
        log.warning("Mod updates channel unreachable guild=%d channel=%s, roundup skipped: %s",
                    guild_id, mod_channel_id, e)


async def handle_approval(interaction: discord.Interaction, suffix: str, approve: bool) -> None:
    from claims import is_moderator
    if not is_moderator(interaction.user):
        await interaction.response.send_message("Only moderators can approve roundups.", ephemeral=True)
        return
    aid = int(suffix)
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM resurface_approvals WHERE id = ?", (aid,)).fetchone()
        if not row or row["status"] != "pending":
            await interaction.response.send_message("This request was already handled.", ephemeral=True)
            return
        conn.execute("UPDATE resurface_approvals SET status = ? WHERE id = ?",
                     ("approved" if approve else "skipped", aid))
        conn.commit()
    row = dict(row)
    who = interaction.user.display_name
    if approve:
        ok = await post_roundup(row["guild_id"], row["channel_id"], "approved")
        done = f"Approved by **{who}**." + ("" if ok else " Nothing left to post.")
    else:
        _save_state(row["channel_id"], row["guild_id"], last_roundup_at=_now().isoformat())
        _counts[row["channel_id"]] = 0
        done = f"Skipped by **{who}**."
    log.info("Roundup approval %s guild=%d channel=%d by=%d", "approved" if approve else "skipped",
             row["guild_id"], row["channel_id"], interaction.user.id)
    await interaction.response.edit_message(view=_approval_view(aid, row["channel_id"], row["codes"], row["trigger"],
                                                                done=done))


async def _expire_approvals() -> None:
    cutoff = (_now() - timedelta(hours=APPROVAL_HOURS)).isoformat()
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM resurface_approvals WHERE status = 'pending' AND created_at <= ?", (cutoff,)).fetchall()]
        conn.executemany("UPDATE resurface_approvals SET status = 'expired' WHERE id = ?", [(r["id"],) for r in rows])
        conn.commit()
    for r in rows:
        _save_state(r["channel_id"], r["guild_id"], last_roundup_at=_now().isoformat())
        _counts[r["channel_id"]] = 0
        ch = await _channel(r["card_channel"] or 0)
        if ch is not None and r.get("card_msg"):
            try:
                await ch.get_partial_message(r["card_msg"]).edit(view=_approval_view(
                    r["id"], r["channel_id"], r["codes"], r["trigger"], done="Skipped. Nobody answered in time."))
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# Trigger loop
# ---------------------------------------------------------------------------

def due(s: dict, channel_id: int, eligible: int) -> str | None:
    if eligible <= 0:
        return None
    st = _state(channel_id)
    last = st.get("last_roundup_at")
    since = (_now() - datetime.fromisoformat(last)).total_seconds() if last else float("inf")
    msgs = _counts.get(channel_id, 0)
    preset = PRESETS.get(s["resurface_activity"])
    if preset:
        need, gap, cap = preset
        today = (st.get("today") or 0) if st.get("day") == _now().strftime("%Y-%m-%d") else 0
        if msgs >= need and since >= gap and today < cap:
            return "activity"
    period = SCHEDULES.get(s["resurface_schedule"])
    if period and since >= period and msgs > 0:
        return "schedule"
    return None


async def check_guild(guild_id: int) -> None:
    s = db.get_settings(guild_id)
    for ch in effective_channels(s):
        rows = open_codes(ch)
        eligible = sum(1 for r in rows if (r.get("resurface_count") or 0) < MAX_FEATURES)
        trigger = due(s, ch, eligible)
        if not trigger:
            continue
        if len(rows) > APPROVAL_THRESHOLD and s["mod_channel_id"]:
            await _request_approval(guild_id, ch, s["mod_channel_id"], trigger, len(rows))
        else:
            await post_roundup(guild_id, ch, trigger)
    if s["resurface_channels"]:
        if s["last_call"]:
            await _last_calls(guild_id, s)
        if s["raffle_reminder"]:
            await _raffle_reminders(guild_id, s)


@tasks.loop(seconds=60)
async def resurface_loop():
    try:
        refresh_enabled()
        with db.get_db() as conn:
            gids = [r[0] for r in conn.execute(
                "SELECT guild_id FROM guild_settings WHERE resurface_channels != ''").fetchall()]
        for gid in gids:
            if _bot.get_guild(gid) is None:
                continue
            await check_guild(gid)
        await _expire_approvals()
        for ch in list(_dirty)[:10]:
            _dirty.discard(ch)
            await _redraw_roundup(ch)
    except Exception:
        log.exception("Resurface loop failed")


# ---------------------------------------------------------------------------
# 60 day cleanup
# ---------------------------------------------------------------------------

def old_codes(guild_id: int) -> list[dict]:
    cutoff = _now() - timedelta(days=CLEANUP_DAYS)
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM shared_codes WHERE guild_id = ?", (guild_id,)).fetchall()]
    return [r for r in rows if posted_at(r) < cutoff]


async def clear_codes(guild_id: int, rows: list[dict], reason: str) -> int:
    by_sharer: dict[int, list[dict]] = {}
    cleared = 0
    for r in rows:
        gone = db.delete_code(r["message_id"])   # row first, so the delete event finds nothing to return
        if gone is None:
            continue
        cleared += 1
        if gone.get("sharer_id"):
            by_sharer.setdefault(gone["sharer_id"], []).append(gone)
        ch = await _channel(gone.get("channel_id") or 0)
        if ch is not None:
            try:
                await ch.get_partial_message(gone["message_id"]).delete()
            except discord.HTTPException:
                pass
        await on_code_gone(gone, "cleared")
        await asyncio.sleep(0.4)
    guild = _bot.get_guild(guild_id)
    where = f" in {guild.name}" if guild else ""
    for sharer_id, items in by_sharer.items():
        try:
            user = _bot.get_user(sharer_id) or await _bot.fetch_user(sharer_id)
            chunk = f"↩️ {reason}{where}, so your unclaimed codes are back with you:\n"
            for it in items:
                line = f"**{cards.item_label(it['item_name'], it.get('platform'))}**\n```\n{it['product_code']}\n```\n"
                if len(chunk) + len(line) > 1900:
                    await user.send(chunk)
                    chunk = ""
                chunk += line
            if chunk:
                await user.send(chunk)
        except discord.HTTPException:
            pass
        await asyncio.sleep(0.5)
    log.info("Cleanup guild=%d cleared=%d sharers=%d reason=%r", guild_id, cleared, len(by_sharer), reason)
    return cleared


@tasks.loop(hours=6)
async def cleanup_loop():
    try:
        with db.get_db() as conn:
            gids = [r[0] for r in conn.execute("SELECT guild_id FROM guild_settings WHERE cleanup_60d = 1").fetchall()]
        for gid in gids:
            if _bot.get_guild(gid) is None:
                continue
            rows = old_codes(gid)
            if rows:
                await clear_codes(gid, rows, f"Codes older than {CLEANUP_DAYS} days were cleared")
    except Exception:
        log.exception("Cleanup loop failed")


# ---------------------------------------------------------------------------
# Previews (private, mod only)
# ---------------------------------------------------------------------------

def backlog_lines(channel_ids) -> list[tuple[int, int, int]]:
    """(channel_id, open codes, codes older than 30 days) for channels that have any."""
    out = []
    month = _now() - timedelta(days=30)
    for ch in channel_ids:
        rows = open_codes(ch)
        if rows:
            out.append((ch, len(rows), sum(1 for r in rows if posted_at(r) < month)))
    return out


class RoundupPreview(ui.LayoutView):
    def __init__(self, guild_id: int, channels: list[int], revert: dict | None):
        super().__init__(timeout=900)
        self.guild_id, self.channels, self.revert = guild_id, channels, revert
        lines = backlog_lines(channels)
        text = "\n".join(f"<#{c}>  ·  {n} open code{'s' if n != 1 else ''}"
                         + (f", {old} older than a month" if old else "") for c, n, old in lines)
        post = ui.Button(label="Post first roundup now", style=discord.ButtonStyle.success)
        wait = ui.Button(label="Wait for the next trigger", style=discord.ButtonStyle.secondary)
        post.callback, wait.callback = self._post, self._wait
        row = [post, wait]
        if revert:
            cancel = ui.Button(label="Undo", style=discord.ButtonStyle.secondary)
            cancel.callback = self._cancel
            row.append(cancel)
        self.add_item(ui.Container(
            ui.TextDisplay("-# RESURFACING PREVIEW"),
            ui.TextDisplay(f"### Here's what will come back\n{text or 'No open codes in these channels right now.'}"),
            ui.TextDisplay("-# Each channel gets one small roundup card. If you close this, it waits for the next "
                           "trigger. Codes only come back in the channel they were posted in."),
            ui.ActionRow(*row),
            accent_color=cards.SKY,
        ))

    async def _post(self, interaction: discord.Interaction):
        from claims import is_moderator
        if not is_moderator(interaction.user):
            return await interaction.response.send_message("Only moderators can do that.", ephemeral=True)
        await interaction.response.edit_message(view=_note("Posting roundups..."))
        posted = 0
        for ch in self.channels:
            posted += await post_roundup(self.guild_id, ch, f"mod:{interaction.user.id}")
        await interaction.edit_original_response(view=_note(f"✅ Posted {posted} roundup{'s' if posted != 1 else ''}."))

    async def _wait(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=_note("Got it. Roundups start when the next trigger hits."))

    async def _cancel(self, interaction: discord.Interaction):
        from claims import is_moderator
        if not is_moderator(interaction.user):
            return await interaction.response.send_message("Only moderators can do that.", ephemeral=True)
        for col, val in (self.revert or {}).items():
            db.set_setting(self.guild_id, col, val)
        await interaction.response.edit_message(view=_note("Undone. Resurfacing is back how it was."))


class CleanupPreview(ui.LayoutView):
    def __init__(self, guild_id: int, turn_on: bool):
        super().__init__(timeout=900)
        self.guild_id, self.turn_on = guild_id, turn_on
        rows = old_codes(guild_id)
        self.n = len(rows)
        chans = len({r.get("channel_id") for r in rows})
        sharers = len({r.get("sharer_id") for r in rows if r.get("sharer_id")})
        if self.n:
            body = (f"### {self.n} code{'s are' if self.n != 1 else ' is'} older than {CLEANUP_DAYS} days\n"
                    f"In {chans} channel{'s' if chans != 1 else ''}, from {sharers} sharer{'s' if sharers != 1 else ''}. "
                    "Their cards will be deleted and each sharer gets their codes back by DM. This can't be undone.")
        else:
            body = f"### Nothing older than {CLEANUP_DAYS} days right now"
        label = (f"Turn on and clear {self.n}" if turn_on else f"Clear {self.n} codes") if self.n else \
            ("Turn on" if turn_on else "Close")
        go = ui.Button(label=label, style=discord.ButtonStyle.danger if self.n else discord.ButtonStyle.primary)
        cancel = ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        go.callback, cancel.callback = self._go, self._cancel
        self.add_item(ui.Container(
            *cards.tile_section(["-# CLEAR OLD CODES", body], cards.TILE_EMPTY, "Empty claw"),
            ui.ActionRow(go, cancel),
            accent_color=cards.SLATE,
        ))

    async def _go(self, interaction: discord.Interaction):
        from claims import is_moderator
        if not is_moderator(interaction.user):
            return await interaction.response.send_message("Only moderators can do that.", ephemeral=True)
        if self.turn_on:
            db.set_setting(self.guild_id, "cleanup_60d", 1)
            log.info("Setting changed guild=%d cleanup_60d=1", self.guild_id)
        if not self.n:
            await interaction.response.edit_message(
                view=_note("Cleanup is on." if self.turn_on else "Nothing to clear."))
            return
        await interaction.response.edit_message(view=_note(f"Clearing {self.n} codes. This can take a few minutes."))
        done = await clear_codes(self.guild_id, old_codes(self.guild_id),
                                 f"A moderator cleared codes older than {CLEANUP_DAYS} days")
        await interaction.edit_original_response(view=_note(f"✅ Cleared {done} codes and returned them to their sharers."))

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=_note("Cancelled. Nothing was cleared."))


def _note(text: str) -> ui.LayoutView:
    v = ui.LayoutView(timeout=60)
    v.add_item(ui.Container(ui.TextDisplay(text), accent_color=cards.SKY))
    return v


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def start(bot) -> None:
    global _bot
    _bot = bot
    refresh_enabled()
    for loop in (resurface_loop, cleanup_loop):
        if not loop.is_running():
            loop.start()


@resurface_loop.before_loop
async def _wait1():
    await _bot.wait_until_ready()


@cleanup_loop.before_loop
async def _wait2():
    await _bot.wait_until_ready()
