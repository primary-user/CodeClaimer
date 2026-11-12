"""
CodeClaimer — private, fair code drops for Discord communities.

Entry point. Wires up logging, the database, slash commands, the component
router, and the background loops. Feature code lives in:
  db.py          storage, migrations, boot backup, atomic claim
  parsing.py     input cleaning, link blocking, expirations, bulk format
  challenges.py  claim challenges
  cards.py       what members see (cards, reveal, DM receipt)
  claims.py      posting drops and the claim flow
  raffle.py      raffle drops, entries and the draw
  refresh.py     one time redraw of pre v2 cards
  resurface.py   roundups, last call, raffle reminders, 60 day cleanup
  welcome.py     welcome card and test drop for newly added servers
  botlists.py    server counts for Top.gg and the other bot lists
  commands.py    slash commands and panels
"""
import asyncio
import logging
import os
import sys

import aiohttp
import discord
from discord.ext import commands, tasks

import botlists
import cards
import claims
import db
import raffle
import refresh
import resurface
import welcome
from commands import ALL_COMMANDS
from parsing import is_expired

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s: %(message)s",
    stream=sys.stdout,
)
logging.getLogger("discord").setLevel(logging.WARNING)
log = logging.getLogger("codeclaimer")

BOT_TOKEN = os.getenv("DISCORD_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")


class CodeBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default(),
                         help_command=None)
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        db.backup_db()
        db.init_db()
        raffle.init_tables()
        raffle.recover_stuck()
        refresh.init_table()
        resurface.init_tables()
        welcome.init_table()
        await cards.ensure_emojis(self)
        claims.bind(self)
        self.http_session = aiohttp.ClientSession()
        for cmd in ALL_COMMANDS:
            self.tree.add_command(cmd)
        try:
            synced = await self.tree.sync()
            log.info("Slash commands synced: %d", len(synced))
        except discord.HTTPException as e:
            log.warning("Command sync failed: %s", e)

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        await super().close()


bot = CodeBot()


# ---------------------------------------------------------------------------
# Component router
# ---------------------------------------------------------------------------
# Claim and reveal buttons are routed here by custom_id instead of registered
# persistent views. One handler then serves every card ever posted, whatever
# its layout, and survives restarts.

ROUTES = {
    cards.CLAIM_CUSTOM_ID:  claims.handle_claim_click,
    cards.MYCODE_CUSTOM_ID: claims.handle_show_my_code,
    raffle.ENTER_CUSTOM_ID: raffle.handle_enter,
    raffle.HOST_CUSTOM_ID:  raffle.handle_host_leftovers,
    resurface.PICK_ID:      resurface.handle_pick,
}

# custom_id prefix -> handler(interaction, suffix). The suffix carries an id.
PREFIX_ROUTES = [
    ("cc:raffle:enter:",          lambda i, x: raffle.handle_enter(i, int(x))),
    (resurface.CLAIMREF_PREFIX,   resurface.handle_claim_ref),
    (resurface.BROWSE_PREFIX,     resurface.handle_browse),
    (resurface.APPROVE_PREFIX,    lambda i, x: resurface.handle_approval(i, x, True)),
    (resurface.SKIP_PREFIX,       lambda i, x: resurface.handle_approval(i, x, False)),
]
ROUTES.update({
    welcome.TEST_ID:     welcome.handle_test,
    welcome.SETTINGS_ID: welcome.handle_settings,
    welcome.HELP_ID:     welcome.handle_help,
})


@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type is not discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    handler = ROUTES.get(custom_id)
    if handler is None:
        for prefix, fn in PREFIX_ROUTES:
            if custom_id.startswith(prefix):
                suffix = custom_id[len(prefix):]
                handler = lambda i, fn=fn, suffix=suffix: fn(i, suffix)  # noqa: E731
                break
    if handler is None:
        return
    try:
        await handler(interaction)
    except Exception:
        log.exception("Component failed custom_id=%s guild=%s user=%s",
                      custom_id, interaction.guild_id, interaction.user.id)
        try:
            text = "Something went wrong. Try again in a moment."
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    log.error("Command /%s failed guild=%s: %r",
              interaction.command.name if interaction.command else "?", interaction.guild_id, error,
              exc_info=error)
    try:
        text = "Something went wrong. Try again in a moment."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------

@tasks.loop(minutes=5)
async def expire_unclaimed_codes():
    try:
        claims.prune_memory()
        expired = [r for r in db.codes_with_expiry() if is_expired(r["expires_at"])]
        if expired:
            log.info("Expiring %d code(s)", len(expired))
        for row in expired:
            await claims.mark_expired(row)
            await asyncio.sleep(0.4)
    except Exception:
        log.exception("Expiration loop failed")


@tasks.loop(minutes=30)
async def post_topgg_stats():
    """Server count to Top.gg and every other bot list with a token set."""
    await botlists.post_all(bot.http_session, bot.user.id, len(bot.guilds))


@tasks.loop(minutes=30)
async def update_presence():
    """Status next to the avatar and on the profile: "42 servers · 1,134 codes dropped" (HotDrop style)."""
    try:
        servers, dropped = len(bot.guilds), db.codes_dropped_total()
        text = f"{servers:,} servers · {dropped:,} codes dropped"
        if text != getattr(bot, "_presence_text", None):
            await bot.change_presence(activity=discord.CustomActivity(name=text))
            bot._presence_text = text
            log.info("Presence updated: %s", text)
    except Exception:
        log.exception("Presence update failed")


@tasks.loop(hours=24)
async def daily_stats():
    try:
        s = db.usage_stats()
        log.info("Stats guilds=%d %s", len(bot.guilds), " ".join(f"{k}={v}" for k, v in s.items()))
        for r in db.live_code_breakdown():
            g = bot.get_guild(r["guild_id"] or 0)
            log.info("Live codes guild=%s name=%r members=%s codes=%d channels=%d with_expiry=%d oldest=%s newest=%s",
                     r["guild_id"], g.name if g else "(left)", g.member_count if g else "-", r["n"],
                     r["channels"], r["with_expiry"], (r["oldest"] or "")[:10], (r["newest"] or "")[:10])
    except Exception:
        log.exception("Stats failed")


for loop in (expire_unclaimed_codes, post_topgg_stats, daily_stats, update_presence):
    @loop.before_loop
    async def _wait_ready():
        await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    db.seed_guild_settings([g.id for g in bot.guilds])
    welcome.mark_existing([g.id for g in bot.guilds])
    members = sum(g.member_count or 0 for g in bot.guilds)
    log.info("Logged in as %s (%d) guilds=%d members=%d db=%s",
             bot.user, bot.user.id, len(bot.guilds), members, db.DB_PATH)
    for loop in (expire_unclaimed_codes, post_topgg_stats, daily_stats, update_presence):
        if not loop.is_running():
            loop.start()
    raffle.start(bot)
    resurface.start(bot)
    asyncio.create_task(refresh.run(bot))


@bot.event
async def on_guild_join(guild: discord.Guild):
    db.seed_guild_settings([guild.id])
    log.info("Joined guild=%d name=%r members=%s total_guilds=%d",
             guild.id, guild.name, guild.member_count, len(bot.guilds))
    await welcome.on_guild_join(guild)
    await update_presence()


@bot.event
async def on_guild_remove(guild: discord.Guild):
    log.info("Left guild=%d name=%r members=%s live_codes=%d total_guilds=%d",
             guild.id, guild.name, guild.member_count, db.live_codes_in_guild(guild.id), len(bot.guilds))
    await update_presence()


@bot.event
async def on_message(message: discord.Message):
    resurface.on_message(message)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    await claims.on_card_deleted(payload.message_id)


@bot.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
    for message_id in payload.message_ids:
        await claims.on_card_deleted(message_id)


async def main():
    async with bot:
        await bot.start(BOT_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
