"""
commands.py — Slash commands, the settings panel, bulk share, claim log, help,
and the Withdraw code message command.
"""
import asyncio
import logging
import uuid

import discord
from discord import app_commands

import cards
import claims
import db
from challenges import MODE_DESCRIPTIONS, VERIFICATION_MODES, _dot_tier_durations
from claims import is_moderator
from parsing import (
    EXPIRY_HELP, clean_platform, contains_link, expiration_is_valid, is_expired,
    normalize_expiration, parse_bulk_text,
)

log = logging.getLogger("codeclaimer.commands")

MODS_ONLY_MSG = "Sharing is set to **mods only** in this server. Ask a moderator to post this drop."
LINK_MSG = "❌ Links and web addresses aren't allowed in drops. This keeps phishing out."


def can_share(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return True
    return (not db.get_settings(interaction.guild.id)["mods_only"]) or is_moderator(interaction.user)


def can_post_here(channel, me) -> bool:
    if me is None or not hasattr(channel, "permissions_for"):
        return True
    p = channel.permissions_for(me)
    return p.view_channel and p.send_messages and p.embed_links


CANT_POST_MSG = ("I can't post in this channel. Give CodeClaimer **View Channel**, **Send Messages** "
                 "and **Embed Links** here, or run this in another channel.")


# ---------------------------------------------------------------------------
# /sharecode
# ---------------------------------------------------------------------------

@app_commands.command(name="sharecode", description="Share a spare code. First person to claim it gets it privately.")
@app_commands.describe(
    item_name="Name of the game or product",
    code="The code. It stays hidden until someone claims it",
    platform="Optional, like Steam, Epic, PS5 or Xbox",
    expires_at="Optional. A date like 12/31/2026, or a length like 24h or 7d",
)
async def sharecode(interaction: discord.Interaction, item_name: str, code: str,
                    platform: str = "", expires_at: str = ""):
    await interaction.response.defer(ephemeral=True)
    if not can_share(interaction):
        await interaction.followup.send(MODS_ONLY_MSG, ephemeral=True)
        return
    if interaction.guild and not can_post_here(interaction.channel, interaction.guild.me):
        await interaction.followup.send(CANT_POST_MSG, ephemeral=True)
        return

    platform = clean_platform(platform)
    expires_at = normalize_expiration(expires_at)
    if any(contains_link(v) for v in (code, item_name, platform, expires_at)):
        await interaction.followup.send(LINK_MSG, ephemeral=True)
        return
    if expires_at and not expiration_is_valid(expires_at):
        await interaction.followup.send(f"❌ I couldn't read that expiration. {EXPIRY_HELP}", ephemeral=True)
        return
    if is_expired(expires_at):
        await interaction.followup.send("❌ That expiration has already passed.", ephemeral=True)
        return

    try:
        await claims.post_claim_card(interaction.channel, interaction.user, item_name.strip()[:200],
                                     platform[:60], code.strip(), expires_at)
    except discord.HTTPException as e:
        log.warning("sharecode post failed guild=%s channel=%s: %s",
                    interaction.guild_id, interaction.channel_id, e)
        await interaction.followup.send(CANT_POST_MSG, ephemeral=True)
        return
    await interaction.followup.send(
        "✅ Your drop is live. Changed your mind? Right click the card, then **Apps → Withdraw code**.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /bulkshare
# ---------------------------------------------------------------------------

BULK_FORMAT_HELP = (
    "One code per line:\n"
    "`Product Name (Platform): CODE | expiration`\n\n"
    "Platform and expiration are optional. Expiration can be a date or a length.\n"
    "`Hollow Knight (Steam): ABC-123 | 12/31/2026`\n"
    "`Celeste: DEF-456 | 7d`\n"
    "`Minecraft Skin Pack (Xbox): GHI-789`"
)


class BulkShareModal(discord.ui.Modal, title="Bulk Share Codes"):
    pre_drop_message = discord.ui.TextInput(
        label="Optional message posted above the drop",
        style=discord.TextStyle.short, required=False, max_length=500,
        placeholder="e.g. Game night drop from @Username! One per person!",
    )
    batch_data = discord.ui.TextInput(
        label="Codes, one per line",
        style=discord.TextStyle.paragraph, required=True, max_length=4000,
        placeholder="Game (Steam): ABC-123 | 12/31/2026\nGame 2: DEF-456 | 7d",
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not can_share(interaction):
            await interaction.followup.send(MODS_ONLY_MSG, ephemeral=True)
            return
        if interaction.guild and not can_post_here(interaction.channel, interaction.guild.me):
            await interaction.followup.send(CANT_POST_MSG, ephemeral=True)
            return

        raw_text = str(self.batch_data.value)
        pre_message = str(self.pre_drop_message.value or "").strip()
        if contains_link(raw_text) or contains_link(pre_message):
            await interaction.followup.send(LINK_MSG, ephemeral=True)
            return

        valid, skipped = parse_bulk_text(raw_text)
        if not valid:
            await interaction.followup.send(f"❌ I couldn't find any codes to post.\n\n{BULK_FORMAT_HELP}",
                                            ephemeral=True)
            return

        await interaction.followup.send(f"Posting **{len(valid)}** codes...", ephemeral=True)
        if pre_message:
            try:
                await interaction.channel.send(
                    pre_message, allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=True),
                )
            except discord.HTTPException as exc:
                log.info("Bulk pre-drop message failed: %s", exc)

        batch_id = str(uuid.uuid4())
        posted = 0
        for item_name, platform, product_code, expires_at in valid:
            try:
                await claims.post_claim_card(interaction.channel, interaction.user, item_name[:200],
                                             platform[:60], product_code, expires_at, batch_id=batch_id)
                posted += 1
            except discord.HTTPException as e:
                skipped.append(f"{item_name}  [couldn't post: {e.status}]")
            await asyncio.sleep(0.6)
        log.info("Bulk drop guild=%s posted=%d skipped=%d batch=%s",
                 interaction.guild_id, posted, len(skipped), batch_id[:8])

        text = f"✅ Posted **{posted}** codes."
        if skipped:
            preview = "\n".join(f"- {e}" for e in skipped[:5])
            more = f"\n...and {len(skipped) - 5} more." if len(skipped) > 5 else ""
            text += f"\n\nSkipped **{len(skipped)}**:\n{preview}{more}"
        await interaction.followup.send(text, ephemeral=True)


class BulkSharePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)

    @discord.ui.button(label="Open Bulk Entry Form", style=discord.ButtonStyle.primary)
    async def open_bulk_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not can_share(interaction):
            await interaction.response.send_message(MODS_ONLY_MSG, ephemeral=True)
            return
        await interaction.response.send_modal(BulkShareModal())


@app_commands.command(name="bulkshare", description="Share many codes at once. Each code gets its own card.")
async def bulkshare(interaction: discord.Interaction):
    if not can_share(interaction):
        await interaction.response.send_message(MODS_ONLY_MSG, ephemeral=True)
        return
    embed = discord.Embed(title="Bulk Share Codes", description=BULK_FORMAT_HELP, color=cards.SKY)
    embed.set_footer(text="You can also add a message that posts above the drop, like a role ping.")
    await interaction.response.send_message(embed=embed, view=BulkSharePanelView(), ephemeral=True)


# ---------------------------------------------------------------------------
# /settings
# ---------------------------------------------------------------------------

def build_settings_embed(guild_id: int) -> discord.Embed:
    s = db.get_settings(guild_id)
    embed = discord.Embed(title="CodeClaimer Settings",
                          description="How CodeClaimer works in this server.", color=cards.SKY)
    embed.add_field(name="Who can share", value=(
        "**Mods only**. Members can claim but not post drops." if s["mods_only"]
        else "**Everyone** can post drops with `/sharecode` and `/bulkshare`."
    ) + "\n-# Mod = Administrator, Manage Server or Manage Messages.", inline=False)
    embed.add_field(name="One claim per bulk drop", value=(
        "**On**. Each member gets one code per `/bulkshare` drop." if s["one_claim_per_batch"]
        else "**Off**. Members can claim several codes from one bulk drop."
    ), inline=False)

    mode = s["verification_mode"]
    detail = ""
    if mode == "difficulty_over_time":
        tiers = " → ".join(f"{int(d)}s" if d else "Easy" for d in _dot_tier_durations(s["dot_start_seconds"]))
        detail = f"\n-# Timer: {tiers}"
    embed.add_field(name="Claim challenge", value=(
        f"**On**, mode **{VERIFICATION_MODES.get(mode, mode)}**. {MODE_DESCRIPTIONS.get(mode, '')}.{detail}"
        if s["claim_verification"] else "**Off**. First click wins."
    ) + "\n-# A wrong answer locks that member out of the card for 15 seconds.", inline=False)

    roles = s["claim_roles"]
    embed.add_field(name="Who can claim", value=(
        "Members with " + " ".join(f"<@&{r}>" for r in roles) + ". Mods always can."
        if roles else "**Everyone**."
    ), inline=False)
    cd = s["claim_cooldown_minutes"]
    embed.add_field(name="Claim cooldown", value=(
        f"**{cd} min** between claims per member." if cd else "**Off**."
    ), inline=False)
    embed.add_field(name="Support", value=(
        f"[Vote on Top.gg]({cards.TOPGG_URL}) · [Ko-fi]({cards.KOFI_URL}) · [Support server]({cards.SUPPORT_URL})"
    ), inline=False)
    return embed


class DOTTimerModal(discord.ui.Modal, title="Difficulty Over Time"):
    start_seconds = discord.ui.TextInput(label="Seconds the Hard tier lasts (10 to 3600)",
                                         style=discord.TextStyle.short, max_length=4, placeholder="60")

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            seconds = int(str(self.start_seconds.value).strip())
            if not 10 <= seconds <= 3600:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Enter a whole number from 10 to 3600.", ephemeral=True)
            return
        db.set_setting(self.guild_id, "dot_start_seconds", seconds)
        await interaction.response.edit_message(embed=build_settings_embed(self.guild_id),
                                                view=SettingsView(self.guild_id))


class CooldownModal(discord.ui.Modal, title="Claim Cooldown"):
    minutes = discord.ui.TextInput(label="Minutes between claims (0 turns it off)",
                                   style=discord.TextStyle.short, max_length=4, placeholder="5")

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            mins = int(str(self.minutes.value).strip())
            if not 0 <= mins <= 1440:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Enter a whole number from 0 to 1440.", ephemeral=True)
            return
        db.set_setting(self.guild_id, "claim_cooldown_minutes", mins)
        await interaction.response.edit_message(embed=build_settings_embed(self.guild_id),
                                                view=SettingsView(self.guild_id))


class SettingsView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        s = db.get_settings(guild_id)

        def toggle(label_on, label_off, on, column, row):
            btn = discord.ui.Button(label=label_on if on else label_off,
                                    style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
                                    row=row)

            async def cb(interaction: discord.Interaction):
                if not await self._mod_check(interaction):
                    return
                current = db.get_settings(self.guild_id)[column]
                db.set_setting(self.guild_id, column, 0 if current else 1)
                log.info("Setting changed guild=%d %s=%s", self.guild_id, column, 0 if current else 1)
                await interaction.response.edit_message(embed=build_settings_embed(self.guild_id),
                                                        view=SettingsView(self.guild_id))
            btn.callback = cb
            self.add_item(btn)

        toggle("Sharing: Mods only", "Sharing: Everyone", s["mods_only"], "mods_only", 0)
        toggle("One per bulk drop: On", "One per bulk drop: Off", s["one_claim_per_batch"], "one_claim_per_batch", 0)
        toggle("Challenge: On", "Challenge: Off", s["claim_verification"], "claim_verification", 0)

        mode = s["verification_mode"]
        select = discord.ui.Select(
            placeholder=f"Challenge mode: {VERIFICATION_MODES.get(mode, mode)}", row=1,
            options=[discord.SelectOption(label=label, value=key, description=MODE_DESCRIPTIONS[key][:100],
                                          default=(key == mode))
                     for key, label in VERIFICATION_MODES.items()],
        )

        async def mode_cb(interaction: discord.Interaction):
            if not await self._mod_check(interaction):
                return
            db.set_setting(self.guild_id, "verification_mode", interaction.data["values"][0])
            await interaction.response.edit_message(embed=build_settings_embed(self.guild_id),
                                                    view=SettingsView(self.guild_id))
        select.callback = mode_cb
        self.add_item(select)

        roles = discord.ui.RoleSelect(placeholder="Who can claim: pick roles, or clear for everyone",
                                      min_values=0, max_values=25, row=2,
                                      default_values=[discord.Object(id=r) for r in s["claim_roles"][:25]])

        async def roles_cb(interaction: discord.Interaction):
            if not await self._mod_check(interaction):
                return
            db.set_setting(self.guild_id, "claim_roles", [int(v) for v in interaction.data.get("values", [])])
            await interaction.response.edit_message(embed=build_settings_embed(self.guild_id),
                                                    view=SettingsView(self.guild_id))
        roles.callback = roles_cb
        self.add_item(roles)

        cd = s["claim_cooldown_minutes"]
        cd_btn = discord.ui.Button(label=f"Cooldown: {cd} min" if cd else "Cooldown: Off",
                                   style=discord.ButtonStyle.secondary, row=3)

        async def cd_cb(interaction: discord.Interaction):
            if await self._mod_check(interaction):
                await interaction.response.send_modal(CooldownModal(self.guild_id))
        cd_btn.callback = cd_cb
        self.add_item(cd_btn)

        if mode == "difficulty_over_time":
            dot_btn = discord.ui.Button(label=f"Hard tier: {s['dot_start_seconds']}s",
                                        style=discord.ButtonStyle.secondary, row=3)

            async def dot_cb(interaction: discord.Interaction):
                if await self._mod_check(interaction):
                    await interaction.response.send_modal(DOTTimerModal(self.guild_id))
            dot_btn.callback = dot_cb
            self.add_item(dot_btn)

    async def _mod_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not is_moderator(interaction.user):
            await interaction.response.send_message("Only moderators can change CodeClaimer settings.",
                                                    ephemeral=True)
            return False
        return True


@app_commands.command(name="settings", description="Open CodeClaimer settings for this server.")
@app_commands.default_permissions(manage_messages=True)
@app_commands.guild_only()
async def settings_command(interaction: discord.Interaction):
    if not is_moderator(interaction.user):
        await interaction.response.send_message("Only moderators can open CodeClaimer settings.", ephemeral=True)
        return
    await interaction.response.send_message(embed=build_settings_embed(interaction.guild.id),
                                            view=SettingsView(interaction.guild.id), ephemeral=True)


# ---------------------------------------------------------------------------
# /claimlog
# ---------------------------------------------------------------------------

class ClaimLogView(discord.ui.View):
    PER_PAGE = 10

    def __init__(self, entries: list, filter_desc: str = ""):
        super().__init__(timeout=180)
        self.entries = entries
        self.filter_desc = filter_desc
        self.page = 0
        self.pages = max(1, (len(entries) + self.PER_PAGE - 1) // self.PER_PAGE)
        self._sync()

    def _sync(self):
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.pages - 1

    def embed(self) -> discord.Embed:
        title = "Claim Log" + (f" · {self.filter_desc}" if self.filter_desc else "")
        if not self.entries:
            return discord.Embed(title=title, description="No claims match those filters yet.", color=cards.SKY)
        start = self.page * self.PER_PAGE
        lines = []
        for i, row in enumerate(self.entries[start:start + self.PER_PAGE], start=start + 1):
            try:
                from datetime import datetime
                when = f"<t:{int(datetime.fromisoformat(row['claimed_at']).timestamp())}:R>"
            except (ValueError, TypeError):
                when = "unknown time"
            kind = " 🎟️" if row.get("source") == "raffle" else ""
            lines.append(
                f"**{i}.** **{cards.item_label(row['item_name'], row['platform'])}**{kind}\n"
                f"-# <@{row['claimer_id']}> ({row['claimer_name']}) · shared by {row['sharer_name'] or 'unknown'} · {when}"
            )
        e = discord.Embed(title=title, description="\n".join(lines), color=cards.SKY)
        e.set_footer(text=f"Page {self.page + 1} of {self.pages} · {len(self.entries)} claims")
        return e

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)


@app_commands.command(name="claimlog", description="See who claimed what in this server. Mods only.")
@app_commands.default_permissions(manage_messages=True)
@app_commands.guild_only()
@app_commands.describe(user="Only claims by this member", item="Only items whose name contains this")
async def claimlog(interaction: discord.Interaction, user: discord.Member | None = None, item: str | None = None):
    if not is_moderator(interaction.user):
        await interaction.response.send_message("Only moderators can view the claim log.", ephemeral=True)
        return
    entries = db.get_claim_log(interaction.guild.id, limit=50,
                               claimer_id=user.id if user else None, item_filter=item)
    parts = []
    if user:
        parts.append(f"by {user.display_name}")
    if item:
        parts.append(f"item: {item}")
    view = ClaimLogView(entries, " · ".join(parts))
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

@app_commands.command(name="help", description="How CodeClaimer works.")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="CodeClaimer",
        description=(
            "Share spare game keys and codes without posting them in public. "
            "The code stays hidden until someone claims it. Then it shows up on their screen, "
            "only to them, with a copy in their DMs."
        ),
        color=cards.SKY,
    )
    embed.add_field(name="/sharecode", value=(
        "Post one code. Add a platform and an expiration if you like.\n"
        "Expiration can be a date (`12/31/2026`) or a length (`24h`, `7d`, `2w`)."
    ), inline=False)
    embed.add_field(name="/bulkshare", value=(
        "Post many codes at once, one card each. With **One claim per bulk drop** on, "
        "each member gets one code per drop."
    ), inline=False)
    embed.add_field(name="Withdraw code", value=(
        "Right click a card (long press on mobile), then **Apps → Withdraw code**. "
        "The sharer gets the code back. Mods can withdraw any drop."
    ), inline=False)
    embed.add_field(name="/settings and /claimlog (mods)", value=(
        "Choose who can share and claim, add a claim challenge or cooldown, and see claim history."
    ), inline=False)
    embed.add_field(name="Safety", value=(
        "Links are blocked in every field. A claim challenge and wrong answer lockout slow down bots. "
        "Claimed and expired cards lock automatically."
    ), inline=False)
    embed.add_field(name="Support", value=(
        f"[Vote on Top.gg]({cards.TOPGG_URL}) · [Ko-fi]({cards.KOFI_URL}) · [Support server]({cards.SUPPORT_URL})"
    ), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Withdraw code (message command)
# ---------------------------------------------------------------------------

@app_commands.context_menu(name="Withdraw code")
@app_commands.guild_only()
async def withdraw_code(interaction: discord.Interaction, message: discord.Message):
    await claims.withdraw(interaction, message)


ALL_COMMANDS = [sharecode, bulkshare, settings_command, claimlog, help_command, withdraw_code]
