"""
CodeClaimer — private, fair code drops for Discord communities.

Entry point. Wires up logging, the database, slash commands, the component
router, and the background loops. Feature code lives in:
  db.py          storage, migrations, boot backup, atomic claim
  parsing.py     input cleaning, link blocking, expirations, bulk format
  challenges.py  claim challenges
  cards.py       what members see (cards, reveal, DM receipt)
  claims.py      posting drops and the claim flow
  commands.py    slash commands and panels
"""
import asyncio
import logging
import os
import sys

import aiohttp
import discord
from discord.ext import commands, tasks

import cards
import claims
import db
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
TOPGG_TOKEN = (os.getenv("TOPGG_TOKEN") or "").strip()


class CodeBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default(),
                         help_command=None)
        self.http_session: aiohttp.ClientSession | None = None
        self._last_topgg_count: int | None = None

    async def setup_hook(self):
        db.backup_db()
        db.init_db()
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
}


@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type is not discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    handler = ROUTES.get(custom_id)
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
    if not TOPGG_TOKEN:
        return
    count = len(bot.guilds)
    try:
        async with bot.http_session.post(
            f"https://top.gg/api/bots/{bot.user.id}/stats",
            json={"server_count": count},
            headers={"Authorization": TOPGG_TOKEN},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status >= 400:
                log.warning("Top.gg stats HTTP %d: %s", resp.status, (await resp.text())[:200])
                return
        if count != bot._last_topgg_count:
            log.info("Top.gg server count posted: %d", count)
            bot._last_topgg_count = count
    except Exception as e:
        log.warning("Top.gg stats failed: %s", e)


@tasks.loop(hours=24)
async def daily_stats():
    try:
        s = db.usage_stats()
        log.info("Stats guilds=%d %s", len(bot.guilds), " ".join(f"{k}={v}" for k, v in s.items()))
    except Exception:
        log.exception("Stats failed")


for loop in (expire_unclaimed_codes, post_topgg_stats, daily_stats):
    @loop.before_loop
    async def _wait_ready():
        await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    db.seed_guild_settings([g.id for g in bot.guilds])
    members = sum(g.member_count or 0 for g in bot.guilds)
    log.info("Logged in as %s (%d) guilds=%d members=%d db=%s",
             bot.user, bot.user.id, len(bot.guilds), members, db.DB_PATH)
    for loop in (expire_unclaimed_codes, post_topgg_stats, daily_stats):
        if not loop.is_running():
            loop.start()


@bot.event
async def on_guild_join(guild: discord.Guild):
    db.seed_guild_settings([guild.id])
    log.info("Joined guild=%d name=%r members=%s total_guilds=%d",
             guild.id, guild.name, guild.member_count, len(bot.guilds))


@bot.event
async def on_guild_remove(guild: discord.Guild):
    log.info("Left guild=%d name=%r members=%s live_codes=%d total_guilds=%d",
             guild.id, guild.name, guild.member_count, db.live_codes_in_guild(guild.id), len(bot.guilds))


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
