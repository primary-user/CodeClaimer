"""
raffle.py — Raffle drops.

/raffle opens a form: a title, one or more codes (same format as bulk), how
long entries stay open, and an optional message to post above it. One card
goes up. Members press Enter (passing the server's claim challenge and role
gate if those are on). When time runs out the draw loop picks random winners,
one code each, flips the card to show the winners and posts a congrats
message that pings them. Winners see their code privately with Show my prize
(same button and log as normal claims), plus a DM copy.

Everything lives in SQLite, so a restart never loses a raffle. A raffle that
ended while the bot was down is drawn as soon as it's back.

Hosts can cancel with Withdraw code (or by deleting the card): codes go back
to the host. Codes nobody won go back to the host too.
"""
import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import ui
from discord.ext import tasks

import cards
import db
from challenges import generate_challenge
from parsing import contains_link, is_expired, parse_bulk_text, parse_expiration_datetime

log = logging.getLogger("codeclaimer.raffle")

ENTER_CUSTOM_ID = "cc:raffle:enter"
HOST_CUSTOM_ID  = "cc:raffle:host"
MAX_CODES       = 20
MIN_SECONDS     = 60
MAX_SECONDS     = 14 * 86400
_rng = random.SystemRandom()
_dirty: set[int] = set()          # raffle message ids whose entry count changed
_bot = None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def init_tables() -> None:
    with db.get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS raffles (
                message_id  INTEGER PRIMARY KEY,
                guild_id    INTEGER NOT NULL,
                channel_id  INTEGER NOT NULL,
                host_id     INTEGER NOT NULL,
                host_name   TEXT,
                title       TEXT NOT NULL DEFAULT '',
                ends_at     TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'open',
                created_at  TEXT NOT NULL,
                drawn_at    TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS raffle_codes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                raffle_id    INTEGER NOT NULL,
                item_name    TEXT NOT NULL,
                platform     TEXT NOT NULL DEFAULT '',
                product_code TEXT NOT NULL,
                expires_at   TEXT NOT NULL DEFAULT '',
                winner_id    INTEGER,
                winner_name  TEXT,
                returned     INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS raffle_entries (
                raffle_id  INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                user_name  TEXT,
                entered_at TEXT NOT NULL,
                PRIMARY KEY (raffle_id, user_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raffles_open ON raffles (status, ends_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raffle_codes_raffle ON raffle_codes (raffle_id)")
        conn.commit()


def get_raffle(message_id: int) -> dict | None:
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM raffles WHERE message_id = ?", (message_id,)).fetchone()
    return dict(row) if row else None


def raffle_codes(raffle_id: int) -> list[dict]:
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM raffle_codes WHERE raffle_id = ? ORDER BY id", (raffle_id,)).fetchall()]


def entry_count(raffle_id: int) -> int:
    with db.get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM raffle_entries WHERE raffle_id = ?", (raffle_id,)).fetchone()[0]


def _set_status(raffle_id: int, new: str, expect: str) -> bool:
    """Compare and set, so only one caller moves a raffle out of a state."""
    with db.get_db() as conn:
        cur = conn.execute("UPDATE raffles SET status = ? WHERE message_id = ? AND status = ?",
                           (new, raffle_id, expect))
        conn.commit()
        return cur.rowcount == 1


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def _prize_lines(codes: list[dict], with_winners: bool = False) -> str:
    lines = []
    for c in codes:
        label = cards.item_label(c["item_name"], c["platform"])
        if with_winners and c.get("winner_id"):
            lines.append(f"🎉 <@{c['winner_id']}> won **{label}**")
        elif with_winners:
            lines.append(f"↩️ **{label}** went back to the host")
        else:
            lines.append(f"**{label}**")
    return "\n".join(lines)


def open_card(r: dict, codes: list[dict], entries: int, challenge: bool) -> ui.LayoutView:
    ends = int(datetime.fromisoformat(r["ends_at"]).timestamp())
    n = len(codes)
    head = f"-# RAFFLE  ·  {n} WINNER{'S' if n != 1 else ''}"
    if challenge:
        head += "  ·  SOLVE A QUICK QUESTION TO ENTER"
    title = r["title"] or ("Giveaway" if n > 1 else cards.item_label(codes[0]["item_name"], codes[0]["platform"]))
    body = f"## {title}"
    if r["title"] or n > 1:
        body += f"\n{_prize_lines(codes)}"
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(
        ui.TextDisplay(head),
        ui.TextDisplay(body),
        ui.TextDisplay(f"Draw <t:{ends}:R>  ·  **{entries}** entered"),
        ui.TextDisplay(f"-# Hosted by <@{r['host_id']}>. Winners are picked at random and see their code privately."),
        ui.Separator(),
        ui.ActionRow(
            ui.Button(label="Enter", style=discord.ButtonStyle.success, custom_id=ENTER_CUSTOM_ID, emoji="🎟️"),
            cards.invite_button(),
        ),
        accent_color=cards.SKY,
    ))
    return view


def ended_card(r: dict, codes: list[dict], entries: int) -> ui.LayoutView:
    title = r["title"] or ("Giveaway" if len(codes) > 1 else cards.item_label(codes[0]["item_name"], codes[0]["platform"]))
    winners = [c for c in codes if c.get("winner_id")]
    leftovers = [c for c in codes if not c.get("winner_id")]
    if winners:
        body = _prize_lines(codes, with_winners=True)
        foot = "-# Winners: press Show my prize to see your code. A copy went to your DMs."
    else:
        body = "Nobody entered, so the codes went back to the host."
        foot = f"-# Hosted by <@{r['host_id']}>."
    row = []
    if winners:
        row.append(ui.Button(label="Show my prize", style=discord.ButtonStyle.primary,
                             custom_id=cards.MYCODE_CUSTOM_ID))
    if leftovers:
        row.append(ui.Button(label="Host: unused codes", style=discord.ButtonStyle.secondary,
                             custom_id=HOST_CUSTOM_ID))
    row.append(cards.invite_button())
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(
        ui.TextDisplay(f"-# RAFFLE ENDED  ·  {entries} ENTERED"),
        ui.TextDisplay(f"## {title}\n{body}"),
        ui.TextDisplay(foot),
        ui.Separator(),
        ui.ActionRow(*row),
        accent_color=cards.NAVY,
    ))
    return view


def cancelled_card(r: dict, codes: list[dict]) -> ui.LayoutView:
    title = r["title"] or (cards.item_label(codes[0]["item_name"], codes[0]["platform"]) if codes else "Raffle")
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(
        ui.TextDisplay("-# RAFFLE CANCELLED"),
        ui.TextDisplay(f"### {title}\nThe host called this one off. The codes went back to them."),
        ui.Separator(),
        ui.ActionRow(cards.invite_button()),
        accent_color=cards.SLATE,
    ))
    return view


async def _edit(r: dict, view: ui.LayoutView) -> None:
    msg = await cards._card_message(_bot, {"channel_id": r["channel_id"], "message_id": r["message_id"]})
    if msg is None:
        return
    try:
        await msg.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        log.info("Raffle card edit failed msg=%d: %s", r["message_id"], e)


# ---------------------------------------------------------------------------
# /raffle form
# ---------------------------------------------------------------------------

def parse_duration(raw: str) -> int | None:
    import re
    m = re.fullmatch(r"\s*(\d{1,4})\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|weeks?)?\s*", raw or "",
                     re.IGNORECASE)
    if not m:
        return None
    unit = (m.group(2) or "h").lower()[0]
    seconds = int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return seconds if MIN_SECONDS <= seconds <= MAX_SECONDS else None


class RaffleModal(discord.ui.Modal, title="Start a Raffle"):
    raffle_title = discord.ui.TextInput(label="Title (optional)", style=discord.TextStyle.short, required=False,
                                        max_length=100, placeholder="e.g. Friday game night giveaway")
    codes = discord.ui.TextInput(
        label="Prizes, one code per line (one winner each)", style=discord.TextStyle.paragraph,
        max_length=4000, placeholder="Hollow Knight (Steam): ABC-123\nCeleste: DEF-456 | 12/31/2026",
    )
    duration = discord.ui.TextInput(label="Entries stay open for", style=discord.TextStyle.short,
                                    default="1h", max_length=10, placeholder="30m, 1h, 2d (1m to 14d)")
    ping = discord.ui.TextInput(label="Message above the raffle (optional)", style=discord.TextStyle.short,
                                required=False, max_length=300, placeholder="e.g. @Members raffle is live!")

    async def on_submit(self, interaction: discord.Interaction):
        from commands import CANT_POST_MSG, LINK_MSG, MODS_ONLY_MSG, can_post_here, can_share
        await interaction.response.defer(ephemeral=True)
        if not can_share(interaction):
            await interaction.followup.send(MODS_ONLY_MSG, ephemeral=True)
            return
        if not can_post_here(interaction.channel, interaction.guild.me):
            await interaction.followup.send(CANT_POST_MSG, ephemeral=True)
            return
        title = str(self.raffle_title.value or "").strip()
        ping = str(self.ping.value or "").strip()
        raw = str(self.codes.value)
        if any(contains_link(t) for t in (title, ping, raw)):
            await interaction.followup.send(LINK_MSG, ephemeral=True)
            return
        seconds = parse_duration(str(self.duration.value))
        if seconds is None:
            await interaction.followup.send("❌ Entries can stay open from 1m to 14d. Try `30m`, `1h` or `2d`.",
                                            ephemeral=True)
            return
        ends_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)

        valid, skipped = parse_bulk_text(raw)
        kept = []
        for item in valid:
            exp = parse_expiration_datetime(item[3])
            if exp and exp <= ends_at:
                skipped.append(f"{item[0]}  [expires before the draw]")
            else:
                kept.append(item)
        if len(kept) > MAX_CODES:
            skipped.extend(f"{k[0]}  [over the {MAX_CODES} prize limit]" for k in kept[MAX_CODES:])
            kept = kept[:MAX_CODES]
        if not kept:
            from commands import BULK_FORMAT_HELP
            note = ("\n\nSkipped:\n" + "\n".join(f"- {s}" for s in skipped[:5])) if skipped else ""
            await interaction.followup.send(f"❌ No prizes to raffle.{note}\n\n{BULK_FORMAT_HELP}", ephemeral=True)
            return

        settings = db.get_settings(interaction.guild_id)
        r = {"message_id": 0, "guild_id": interaction.guild_id, "channel_id": interaction.channel_id,
             "host_id": interaction.user.id, "host_name": interaction.user.display_name,
             "title": title[:100], "ends_at": ends_at.isoformat(), "status": "open"}
        code_rows = [{"item_name": i[0][:200], "platform": i[1][:60], "product_code": i[2], "expires_at": i[3]}
                     for i in kept]

        if ping:
            try:
                await interaction.channel.send(
                    ping, allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=True))
            except discord.HTTPException as e:
                log.info("Raffle ping failed: %s", e)
        try:
            msg = await interaction.channel.send(
                view=open_card(r, code_rows, 0, bool(settings["claim_verification"])),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as e:
            log.warning("Raffle post failed guild=%s: %s", interaction.guild_id, e)
            await interaction.followup.send(CANT_POST_MSG, ephemeral=True)
            return

        with db.get_db() as conn:
            conn.execute(
                """INSERT INTO raffles (message_id, guild_id, channel_id, host_id, host_name, title, ends_at,
                                        status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
                (msg.id, r["guild_id"], r["channel_id"], r["host_id"], r["host_name"], r["title"], r["ends_at"],
                 db.now_iso()),
            )
            conn.executemany(
                """INSERT INTO raffle_codes (raffle_id, item_name, platform, product_code, expires_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [(msg.id, c["item_name"], c["platform"], c["product_code"], c["expires_at"]) for c in code_rows],
            )
            conn.commit()
        log.info("Raffle started guild=%s msg=%d host=%d prizes=%d minutes=%d",
                 interaction.guild_id, msg.id, interaction.user.id, len(code_rows), seconds // 60)

        ts = int(ends_at.timestamp())
        text = (f"✅ Raffle is live with **{len(code_rows)}** prize{'s' if len(code_rows) != 1 else ''}. "
                f"Draw <t:{ts}:R>.\n-# To cancel, right click the card, then **Apps → Withdraw code**.")
        if skipped:
            text += "\n\nSkipped:\n" + "\n".join(f"- {s}" for s in skipped[:5])
        await interaction.followup.send(text, ephemeral=True)


async def raffle_command(interaction: discord.Interaction):
    from commands import MODS_ONLY_MSG, can_share
    if not can_share(interaction):
        await interaction.response.send_message(MODS_ONLY_MSG, ephemeral=True)
        return
    await interaction.response.send_modal(RaffleModal())


# ---------------------------------------------------------------------------
# Entering
# ---------------------------------------------------------------------------

def _add_entry(raffle_id: int, user_id: int, user_name: str) -> str:
    """Returns ok | already | closed. Single transaction, safe against double clicks."""
    with db.get_db() as conn:
        open_ = conn.execute("SELECT status, ends_at FROM raffles WHERE message_id = ?", (raffle_id,)).fetchone()
        if not open_ or open_["status"] != "open" or \
                datetime.fromisoformat(open_["ends_at"]) <= datetime.now(timezone.utc):
            return "closed"
        cur = conn.execute(
            "INSERT OR IGNORE INTO raffle_entries (raffle_id, user_id, user_name, entered_at) VALUES (?, ?, ?, ?)",
            (raffle_id, user_id, user_name, db.now_iso()),
        )
        conn.commit()
        return "ok" if cur.rowcount == 1 else "already"


def _remove_entry(raffle_id: int, user_id: int) -> bool:
    with db.get_db() as conn:
        status = conn.execute("SELECT status FROM raffles WHERE message_id = ?", (raffle_id,)).fetchone()
        if not status or status["status"] != "open":
            return False
        cur = conn.execute("DELETE FROM raffle_entries WHERE raffle_id = ? AND user_id = ?", (raffle_id, user_id))
        conn.commit()
        return cur.rowcount == 1


class LeaveView(discord.ui.View):
    def __init__(self, raffle_id: int):
        super().__init__(timeout=120)
        self.raffle_id = raffle_id

    @discord.ui.button(label="Leave raffle", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if _remove_entry(self.raffle_id, interaction.user.id):
            _dirty.add(self.raffle_id)
            await interaction.response.edit_message(content="You left the raffle.", view=None)
        else:
            await interaction.response.edit_message(content="This raffle has already closed.", view=None)


class EntryChallengeView(discord.ui.View):
    def __init__(self, raffle_id: int, answer: str, choices: list[str]):
        super().__init__(timeout=60)
        self.raffle_id = raffle_id
        self.answer = answer
        self.done = False
        for choice in choices:
            btn = discord.ui.Button(label=str(choice)[:80], style=discord.ButtonStyle.secondary)
            btn.callback = self._cb(str(choice))
            self.add_item(btn)

    def _cb(self, choice: str):
        async def callback(interaction: discord.Interaction):
            from claims import WRONG_ANSWER_LOCKOUT, _lockouts
            if self.done:
                await interaction.response.send_message("You already answered this one.", ephemeral=True)
                return
            self.done = True
            self.stop()
            if choice != self.answer:
                _lockouts[(self.raffle_id, interaction.user.id)] = time.monotonic() + WRONG_ANSWER_LOCKOUT
                await interaction.response.edit_message(
                    content=f"❌ Not quite. You can try again in {WRONG_ANSWER_LOCKOUT} seconds.", view=None)
                return
            await interaction.response.edit_message(
                content=_entry_result(self.raffle_id, interaction.user), view=None)
        return callback


def _entry_result(raffle_id: int, user) -> str:
    status = _add_entry(raffle_id, user.id, user.display_name)
    if status == "ok":
        _dirty.add(raffle_id)
        log.info("Raffle entry msg=%d user=%d", raffle_id, user.id)
        r = get_raffle(raffle_id)
        ts = int(datetime.fromisoformat(r["ends_at"]).timestamp()) if r else 0
        return f"🎟️ You're in! The draw is <t:{ts}:R>. Winners get pinged here."
    if status == "already":
        return "You're already in this raffle."
    return "This raffle has closed."


async def handle_enter(interaction: discord.Interaction) -> None:
    from claims import _lockouts, format_wait, is_moderator
    r = get_raffle(interaction.message.id)
    if not r or r["status"] != "open" or datetime.fromisoformat(r["ends_at"]) <= datetime.now(timezone.utc):
        await interaction.response.send_message("This raffle has closed. The draw is on its way.", ephemeral=True)
        return
    if interaction.user.id == r["host_id"]:
        await interaction.response.send_message("You're hosting this one, so you can't enter it.", ephemeral=True)
        return
    with db.get_db() as conn:
        already = conn.execute("SELECT 1 FROM raffle_entries WHERE raffle_id = ? AND user_id = ?",
                               (r["message_id"], interaction.user.id)).fetchone()
    if already:
        await interaction.response.send_message("You're already in this raffle.", view=LeaveView(r["message_id"]),
                                                ephemeral=True)
        return

    settings = db.get_settings(interaction.guild_id)
    until = _lockouts.get((r["message_id"], interaction.user.id), 0)
    if until > time.monotonic():
        await interaction.response.send_message(
            f"Wrong answer. You can try again in **{format_wait(until - time.monotonic())}**.", ephemeral=True)
        return
    roles = settings["claim_roles"]
    if roles and not is_moderator(interaction.user):
        if not {x.id for x in getattr(interaction.user, "roles", [])} & set(roles):
            mentions = " ".join(f"<@&{x}>" for x in roles[:5])
            await interaction.response.send_message(f"Entering here needs one of these roles: {mentions}",
                                                    ephemeral=True)
            return

    if settings["claim_verification"]:
        question, answer, choices = generate_challenge(settings["verification_mode"], 9999,
                                                       settings["dot_start_seconds"])
        await interaction.response.send_message(
            f"**{question}**\n-# Answer within 60 seconds to enter.",
            view=EntryChallengeView(r["message_id"], answer, choices), ephemeral=True)
        return
    await interaction.response.send_message(_entry_result(r["message_id"], interaction.user), ephemeral=True)


async def handle_host_leftovers(interaction: discord.Interaction) -> None:
    r = get_raffle(interaction.message.id)
    if not r or interaction.user.id != r["host_id"]:
        await interaction.response.send_message("Only the host can see unused codes.", ephemeral=True)
        return
    left = [c for c in raffle_codes(r["message_id"]) if not c.get("winner_id")]
    if not left:
        await interaction.response.send_message("Every code found a winner.", ephemeral=True)
        return
    lines = "\n".join(f"**{cards.item_label(c['item_name'], c['platform'])}**\n```\n{c['product_code']}\n```"
                      for c in left)
    await interaction.response.send_message(f"Unused codes from your raffle:\n{lines}"[:1990], ephemeral=True)


# ---------------------------------------------------------------------------
# Draw
# ---------------------------------------------------------------------------

async def draw(raffle_id: int) -> None:
    if not _set_status(raffle_id, "drawing", "open"):
        return
    r = get_raffle(raffle_id)
    codes = raffle_codes(raffle_id)
    with db.get_db() as conn:
        entrants = [dict(e) for e in conn.execute(
            "SELECT user_id, user_name FROM raffle_entries WHERE raffle_id = ?", (raffle_id,)).fetchall()]

    usable = [c for c in codes if not c.get("winner_id") and not is_expired(c["expires_at"])]
    picked = _rng.sample(entrants, min(len(entrants), len(usable)))
    _rng.shuffle(usable)
    pairs = list(zip(picked, usable))

    with db.get_db() as conn:
        ts = db.now_iso()
        for winner, code in pairs:
            conn.execute("UPDATE raffle_codes SET winner_id = ?, winner_name = ? WHERE id = ?",
                         (winner["user_id"], winner["user_name"], code["id"]))
            conn.execute(
                """INSERT INTO claims_log (guild_id, channel_id, message_id, claimer_id, claimer_name, item_name,
                                           platform, sharer_id, sharer_name, claimed_at, product_code, expires_at,
                                           source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'raffle')""",
                (r["guild_id"], r["channel_id"], raffle_id, winner["user_id"], winner["user_name"] or "",
                 code["item_name"], code["platform"], r["host_id"], r["host_name"], ts, code["product_code"],
                 code["expires_at"]),
            )
        conn.execute("UPDATE raffles SET status = 'done', drawn_at = ? WHERE message_id = ?", (ts, raffle_id))
        conn.commit()

    codes = raffle_codes(raffle_id)
    await _edit(r, ended_card(r, codes, len(entrants)))
    log.info("Raffle drawn guild=%s msg=%d entries=%d winners=%d leftovers=%d",
             r["guild_id"], raffle_id, len(entrants), len(pairs), len(codes) - len(pairs))

    guild = _bot.get_guild(r["guild_id"])
    if pairs:
        channel = _bot.get_channel(r["channel_id"])
        if channel is not None:
            mentions = " ".join(f"<@{w['user_id']}>" for w, _ in pairs)
            try:
                await channel.send(
                    f"🎉 Congrats {mentions}! Press **Show my prize** on the raffle card to see your code.",
                    reference=discord.MessageReference(message_id=raffle_id, channel_id=r["channel_id"],
                                                       fail_if_not_exists=False),
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False,
                                                             replied_user=False),
                )
            except discord.HTTPException as e:
                log.info("Raffle congrats failed msg=%d: %s", raffle_id, e)
        for winner, code in pairs:
            try:
                user = _bot.get_user(winner["user_id"]) or await _bot.fetch_user(winner["user_id"])
                await user.send(embed=cards.dm_receipt_embed(
                    code["item_name"], code["platform"], code["product_code"], code["expires_at"],
                    guild.name if guild else None, r["host_name"], raffle=True,
                ), view=cards.dm_receipt_view())
            except discord.HTTPException:
                pass
            await asyncio.sleep(0.3)

    leftovers = [c for c in codes if not c.get("winner_id")]
    if leftovers:
        await _return_codes(r, leftovers, "Your raffle ended with codes nobody won")


async def _return_codes(r: dict, codes: list[dict], reason: str) -> bool:
    try:
        user = _bot.get_user(r["host_id"]) or await _bot.fetch_user(r["host_id"])
        lines = "\n".join(f"**{cards.item_label(c['item_name'], c['platform'])}**\n```\n{c['product_code']}\n```"
                          for c in codes)
        guild = _bot.get_guild(r["guild_id"])
        where = f" in {guild.name}" if guild else ""
        await user.send(f"↩️ {reason}{where}. Here they are:\n{lines}"[:1990])
        return True
    except discord.HTTPException:
        return False


# ---------------------------------------------------------------------------
# Cancel (Withdraw code or card deleted)
# ---------------------------------------------------------------------------

async def cancel(raffle_id: int) -> dict | None:
    """Close an open raffle without a draw. Returns the raffle row if this call cancelled it."""
    if not _set_status(raffle_id, "cancelled", "open"):
        return None
    r = get_raffle(raffle_id)
    with db.get_db() as conn:
        conn.execute("UPDATE raffle_codes SET returned = 1 WHERE raffle_id = ?", (raffle_id,))
        conn.commit()
    log.info("Raffle cancelled guild=%s msg=%d", r["guild_id"], raffle_id)
    return r


async def withdraw(interaction: discord.Interaction, message: discord.Message) -> bool:
    """Called by Withdraw code. Returns False if the message isn't an open raffle."""
    from claims import is_moderator
    r = get_raffle(message.id)
    if not r or r["status"] != "open":
        return False
    is_host = interaction.user.id == r["host_id"]
    if not (is_host or is_moderator(interaction.user)):
        await interaction.response.send_message("Only the host or a moderator can cancel this raffle.",
                                                ephemeral=True)
        return True
    r = await cancel(message.id)
    if r is None:
        await interaction.response.send_message("Too late, this raffle is already being drawn.", ephemeral=True)
        return True
    codes = raffle_codes(message.id)
    await _edit(r, cancelled_card(r, codes))
    if is_host:
        lines = "\n".join(f"**{cards.item_label(c['item_name'], c['platform'])}**\n```\n{c['product_code']}\n```"
                          for c in codes)
        await interaction.response.send_message(f"↩️ Raffle cancelled. Your codes:\n{lines}"[:1990], ephemeral=True)
    else:
        sent = await _return_codes(r, codes, "A moderator cancelled your raffle")
        await interaction.response.send_message(
            "Raffle cancelled. " + ("The codes were sent back to the host by DM." if sent
                                    else "I couldn't DM the host, so the codes are gone."), ephemeral=True)
    return True


async def on_card_deleted(message_id: int) -> bool:
    r = await cancel(message_id)
    if r is None:
        return False
    await _return_codes(r, raffle_codes(message_id), "Your raffle card was deleted")
    return True


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------

@tasks.loop(seconds=15)
async def raffle_loop():
    try:
        now = datetime.now(timezone.utc).isoformat()
        with db.get_db() as conn:
            due = [row[0] for row in conn.execute(
                "SELECT message_id FROM raffles WHERE status = 'open' AND ends_at <= ?", (now,)).fetchall()]
        for rid in due:
            _dirty.discard(rid)
            await draw(rid)

        # Entry counts, at most one edit per raffle per tick
        for rid in list(_dirty)[:10]:
            _dirty.discard(rid)
            r = get_raffle(rid)
            if r and r["status"] == "open":
                settings = db.get_settings(r["guild_id"])
                await _edit(r, open_card(r, raffle_codes(rid), entry_count(rid),
                                         bool(settings["claim_verification"])))
                await asyncio.sleep(0.5)
    except Exception:
        log.exception("Raffle loop failed")


@raffle_loop.before_loop
async def _before():
    await _bot.wait_until_ready()


def start(bot) -> None:
    global _bot
    _bot = bot
    if not raffle_loop.is_running():
        raffle_loop.start()


def recover_stuck() -> None:
    """A raffle left in 'drawing' by a crash goes back to open so the loop draws it.
    Winners are written in one transaction with the 'done' status, so a raffle
    still marked 'drawing' has no winners saved yet."""
    with db.get_db() as conn:
        n = conn.execute("UPDATE raffles SET status = 'open' WHERE status = 'drawing'").rowcount
        conn.commit()
    if n:
        log.warning("Recovered %d raffle(s) stuck mid draw", n)
