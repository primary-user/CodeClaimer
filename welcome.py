"""
Welcome card for servers that add CodeClaimer from now on.

Posts once per server, ever: in the system channel, or the first text
channel the bot can post in. Its three buttons are mod only:
  Post a test drop   a fake code on a real claim card, kept out of every stat
  Open settings      the /settings panel, privately
  How it works       the /help panel, privately
"""
import logging
import random
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import ui

import cards
import db
from claims import is_moderator

log = logging.getLogger("codeclaimer.welcome")

TEST_ID = "cc:welcome:test"
SETTINGS_ID = "cc:welcome:settings"
HELP_ID = "cc:welcome:help"

TEST_ITEM = "Practice Code"
TEST_NOTE = "Not a real key. Claim it to see how a drop works."
TEST_COOLDOWN = 600          # seconds between test drops per server
MODS_ONLY = "Only moderators can use these buttons."

_last_test: dict[int, float] = {}


def init_table() -> None:
    with db.get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS welcomed_guilds (
                guild_id   INTEGER PRIMARY KEY,
                channel_id INTEGER,
                message_id INTEGER,
                posted_at  TEXT NOT NULL
            )""")
        conn.commit()


def mark_existing(guild_ids: list[int]) -> None:
    """Servers the bot is already in never get the card, even after a rejoin."""
    if not guild_ids:
        return
    with db.get_db() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO welcomed_guilds (guild_id, posted_at) VALUES (?, ?)",
            [(g, "existing") for g in guild_ids])
        conn.commit()


def _already_welcomed(guild_id: int) -> bool:
    with db.get_db() as conn:
        return conn.execute("SELECT 1 FROM welcomed_guilds WHERE guild_id = ?", (guild_id,)).fetchone() is not None


def _record(guild_id: int, channel_id: int | None, message_id: int | None) -> None:
    with db.get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO welcomed_guilds (guild_id, channel_id, message_id, posted_at) VALUES (?, ?, ?, ?)",
            (guild_id, channel_id, message_id, db.now_iso()))
        conn.commit()


def _can_post(channel, me) -> bool:
    if channel is None or me is None or not hasattr(channel, "permissions_for"):
        return False
    p = channel.permissions_for(me)
    return p.view_channel and p.send_messages


def pick_channel(guild):
    me = guild.me
    if _can_post(guild.system_channel, me):
        return guild.system_channel
    for ch in sorted(getattr(guild, "text_channels", []), key=lambda c: getattr(c, "position", 0)):
        if _can_post(ch, me):
            return ch
    return None


def welcome_card() -> ui.LayoutView:
    view = ui.LayoutView(timeout=None)
    code = lambda c: f"`{c}`"  # noqa: E731
    view.add_item(ui.Container(
        *cards.header("THANKS FOR ADDING CODECLAIMER", "## Share codes without posting them in public",
                      "Built and run by one person. Three steps to your first drop:",
                      cards.TILE_LIVE, "Claw reaching for a gift"),
        ui.TextDisplay(
            f"**1.** Run {code('/sharecode')} in any channel. The code stays hidden on a claim card.\n"
            "**2.** The first member to claim it sees the code on their screen, only them.\n"
            f"**3.** Got a pile of keys? {code('/bulkshare')} posts many, {code('/raffle')} draws random winners."
        ),
        ui.TextDisplay("-# Sharing starts as mods only, with a quick question before each claim to stop bots. "
                       "Change both in /settings."),
        ui.Separator(),
        ui.ActionRow(
            ui.Button(label="Post a test drop", style=discord.ButtonStyle.success, custom_id=TEST_ID),
            ui.Button(label="Open settings", style=discord.ButtonStyle.secondary, custom_id=SETTINGS_ID),
            ui.Button(label="How it works", style=discord.ButtonStyle.secondary, custom_id=HELP_ID),
        ),
        ui.TextDisplay(f"-# Questions? [Support server]({cards.SUPPORT_URL})"),
        accent_color=cards.SKY,
    ))
    return view


async def on_guild_join(guild) -> None:
    """Fail safe: a welcome problem never breaks the join."""
    try:
        if _already_welcomed(guild.id):
            log.info("Welcome skipped guild=%d (seen before)", guild.id)
            return
        channel = pick_channel(guild)
        if channel is None:
            _record(guild.id, None, None)
            log.info("Welcome skipped guild=%d (no channel I can post in)", guild.id)
            return
        msg = await channel.send(view=welcome_card())
        _record(guild.id, channel.id, msg.id)
        log.info("Welcome posted guild=%d channel=%d", guild.id, channel.id)
    except Exception:
        log.exception("Welcome failed guild=%s", getattr(guild, "id", None))


async def _mods_only(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or not is_moderator(interaction.user):
        await interaction.response.send_message(MODS_ONLY, ephemeral=True)
        return False
    return True


async def handle_test(interaction: discord.Interaction) -> None:
    if not await _mods_only(interaction):
        return
    gid = interaction.guild.id
    wait = TEST_COOLDOWN - (time.monotonic() - _last_test.get(gid, -TEST_COOLDOWN))
    if wait > 0:
        await interaction.response.send_message(
            f"A test drop went out a moment ago. Try again in {int(wait // 60) + 1} min.", ephemeral=True)
        return
    channel = interaction.channel
    if not _can_post(channel, getattr(interaction.guild, "me", None)):
        await interaction.response.send_message(
            "I can't post in this channel. Give CodeClaimer **View Channel** and **Send Messages** here.",
            ephemeral=True)
        return
    _last_test[gid] = time.monotonic()
    await interaction.response.defer(ephemeral=True, thinking=True)
    import claims  # late import: claims binds the bot at startup
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    await claims.post_claim_card(
        channel, interaction.user, TEST_ITEM, "", f"CLAW-TEST-{random.randint(1000, 9999)}",
        expires_at=expires, label="TEST DROP", note=TEST_NOTE, is_test=True)
    log.info("Test drop posted guild=%d channel=%d by=%d", gid, channel.id, interaction.user.id)
    await interaction.followup.send(
        "Test drop posted. Claim it to see what members see. It isn't counted anywhere and expires in 24 hours.",
        ephemeral=True)


async def handle_settings(interaction: discord.Interaction) -> None:
    if not await _mods_only(interaction):
        return
    from commands import SettingsPanel
    await interaction.response.send_message(view=SettingsPanel(interaction.guild.id), ephemeral=True)


async def handle_help(interaction: discord.Interaction) -> None:
    if not await _mods_only(interaction):
        return
    from commands import help_panel
    await interaction.response.send_message(view=help_panel(), ephemeral=True)
