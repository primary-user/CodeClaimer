import os
import re
import random
import asyncio
import sqlite3
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
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

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


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shared_codes (
                message_id  INTEGER PRIMARY KEY,
                product_code TEXT NOT NULL,
                item_name    TEXT NOT NULL,
                platform     TEXT NOT NULL DEFAULT '',
                expires_at   TEXT NOT NULL DEFAULT '',
                guild_id     INTEGER,
                channel_id   INTEGER,
                sharer_id    INTEGER,
                created_at   TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id  INTEGER PRIMARY KEY,
                mods_only INTEGER NOT NULL DEFAULT 1
            )
        """)

        # Forward migrations — safe to run on existing databases
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(shared_codes)")
        }
        for col, definition in [
            ("guild_id",   "INTEGER"),
            ("channel_id", "INTEGER"),
            ("sharer_id",  "INTEGER"),
            ("created_at", "TEXT"),
            ("expires_at", "TEXT NOT NULL DEFAULT ''"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE shared_codes ADD COLUMN {col} {definition}")

        conn.commit()


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(
    r"(https?://[^\s]+)|(www\.[^\s]+)|([a-zA-Z0-9-]+\.[a-zA-Z]{2,}(/[^\s]*)?)"
)
_JUNK_VALUES = {"unknown", "n/a", "na", "none", "no platform",
                "no expiration", "never", "no expiry", "no expire"}


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
    """Return a UTC datetime for the expiration string, or None if blank/invalid."""
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
            # Date-only: expires at end of that day (midnight next day UTC)
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
# Card description builders
# ---------------------------------------------------------------------------

def format_platform_line(platform: str | None) -> str:
    p = clean_platform(platform)
    return f"**Platform:** {p}\n" if p else ""


def format_expiration_line(expires_at: str | None) -> str:
    e = clean_expiration(expires_at)
    return f"**Expires:** {e}\n" if e else ""


def build_expired_embed(item_name: str, platform: str, expires_at: str) -> discord.Embed:
    display_platform = f" ({clean_platform(platform)})" if clean_platform(platform) else ""
    return discord.Embed(
        title="Code Expired",
        description=(
            f"The code for **{item_name}**{display_platform} "
            f"was not claimed before it expired.\n\n"
            f"**Expired:** {clean_expiration(expires_at)}"
        ),
        color=discord.Color.dark_grey(),
    )


def build_claimed_embed(
    item_name: str,
    platform: str,
    claimer_mention: str,
    sharer_mention: str,
) -> discord.Embed:
    display_platform = f" ({clean_platform(platform)})" if clean_platform(platform) else ""
    return discord.Embed(
        title="Loot Claimed!",
        description=(
            f"The code for **{item_name}**{display_platform} has been successfully "
            f"claimed by {claimer_mention}!\n\n"
            f"Thank you to {sharer_mention} for sharing with the community!"
        ),
        color=discord.Color.dark_grey(),
    )


# ---------------------------------------------------------------------------
# Bulk parsing helpers
# ---------------------------------------------------------------------------

def parse_bulk_label(label: str) -> tuple[str, str]:
    """Split 'Product Name (Platform)' into (item_name, platform)."""
    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", label.strip())
    if m:
        return m.group(1).strip(), clean_platform(m.group(2))
    return label.strip(), ""


def parse_bulk_entry(entry: str) -> tuple[str, str, str, str] | None:
    """
    Parse one line:  Product Name (Platform): Code | MM/DD/YYYY
    Returns (item_name, platform, product_code, expires_at) or None.
    """
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
        expires_at = clean_expiration(expires_at)
    else:
        product_code, expires_at = rest.strip(), ""

    item_name, platform = parse_bulk_label(raw_label)
    if not item_name or not product_code:
        return None

    return item_name, platform, product_code, expires_at


def split_bulk_entries(raw_text: str) -> list[str]:
    """Split on newlines (preferred) or semicolons (fallback)."""
    if "\n" in raw_text:
        return [l.strip() for l in raw_text.splitlines() if l.strip()]
    if ";" in raw_text:
        return [e.strip() for e in raw_text.split(";") if e.strip()]
    return [raw_text.strip()] if raw_text.strip() else []


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------

def is_moderator(user) -> bool:
    perms = getattr(user, "guild_permissions", None)
    if perms is None:
        return False
    return perms.administrator or perms.manage_guild or perms.manage_messages


def get_mods_only(guild_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT mods_only FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO guild_settings (guild_id, mods_only) VALUES (?, 1)", (guild_id,)
            )
            conn.commit()
            return True
        return bool(row["mods_only"])


def set_mods_only(guild_id: int, enabled: bool) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO guild_settings (guild_id, mods_only) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET mods_only = excluded.mods_only
            """,
            (guild_id, 1 if enabled else 0),
        )
        conn.commit()


async def user_can_use_bot(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return True
    return (not get_mods_only(interaction.guild.id)) or is_moderator(interaction.user)


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

    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO shared_codes
                (message_id, product_code, item_name, platform, expires_at,
                 guild_id, channel_id, sharer_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg.id, product_code, item_name, platform, expires_at,
                getattr(getattr(channel, "guild", None), "id", None),
                getattr(channel, "id", None),
                getattr(sharer, "id", None),
                datetime.now(timezone.utc).isoformat(),
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
    """Edit the public card to show it has expired, then remove it from the DB."""

    # Always remove the DB row first so a second click or task run cannot race
    with get_db() as conn:
        conn.execute("DELETE FROM shared_codes WHERE message_id = ?", (message_id,))
        conn.commit()

    # Attempt to update the Discord message
    channel = bot.get_channel(channel_id) if channel_id else None
    if channel is None and channel_id:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            print(f"[Expiration] Cannot fetch channel {channel_id} "
                  f"for message {message_id}: {exc}")

    if channel is None:
        print(f"[Expiration] No channel available for message {message_id} — "
              "DB row removed but Discord card not updated.")
        return

    try:
        msg = await channel.fetch_message(message_id)
        await msg.edit(
            embed=build_expired_embed(item_name, platform, expires_at),
            view=None,
        )
        print(f"[Expiration] Marked message {message_id} as expired.")
    except discord.NotFound:
        print(f"[Expiration] Message {message_id} already deleted — nothing to edit.")
    except discord.Forbidden:
        print(f"[Expiration] Missing permissions to edit message {message_id} "
              f"in channel {channel_id}.")
    except discord.HTTPException as exc:
        print(f"[Expiration] HTTP error editing message {message_id}: {exc}")


# ---------------------------------------------------------------------------
# Views & Modals
# ---------------------------------------------------------------------------

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
        msg_id = interaction.message.id

        with get_db() as conn:
            row = conn.execute(
                """
                SELECT product_code, item_name, platform, expires_at, channel_id
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
        else:
            # Fallback: in-memory values from when the view was created
            code_to_send     = self.product_code
            item_to_send     = self.item_name
            platform_to_send = self.platform
            expires_to_send  = self.expires_at
            channel_id       = getattr(interaction.channel, "id", None)

        if not code_to_send:
            await interaction.response.send_message(
                "This code has already been claimed!", ephemeral=True
            )
            return

        # Handle expired card clicked before the background task caught it
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

        display_platform = f" ({platform_to_send})" if platform_to_send else ""

        # Send DM first — if it fails we do not mark anything as claimed
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
            value="Have extra keys? Use `/sharecode` to pay it forward!",
            inline=False,
        )

        try:
            await interaction.user.send(embed=dm_embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                "Could not send you a DM. Please open your Privacy Settings / DMs and try again.",
                ephemeral=True,
            )
            return

        # DM delivered — now commit the claim
        with get_db() as conn:
            conn.execute("DELETE FROM shared_codes WHERE message_id = ?", (msg_id,))
            conn.commit()

        await interaction.response.send_message(
            "The code has been sent to your DMs!", ephemeral=True
        )

        # Extract sharer mention from the existing embed description
        sharer_mention = "Someone"
        if interaction.message.embeds and interaction.message.embeds[0].description:
            for line in interaction.message.embeds[0].description.split("\n"):
                if "Shared by:" in line:
                    sharer_mention = line.replace("**Shared by:**", "").strip()
                    break

        try:
            await interaction.message.edit(
                embed=build_claimed_embed(
                    item_name=item_to_send,
                    platform=platform_to_send,
                    claimer_mention=interaction.user.mention,
                    sharer_mention=sharer_mention,
                ),
                view=None,
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            print(f"[Claim] Could not update public card for message {msg_id}: {exc}")


class SettingsView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id

        mods_only = get_mods_only(guild_id)
        toggle = discord.ui.Button(
            label="Mods Only: ON" if mods_only else "Mods Only: OFF",
            style=discord.ButtonStyle.danger if mods_only else discord.ButtonStyle.success,
            custom_id="codeclaimer_toggle_mods_only",
        )
        toggle.callback = self._toggle_callback
        self.add_item(toggle)

        self.add_item(discord.ui.Button(
            label="Support CodeClaimer",
            style=discord.ButtonStyle.link,
            url="https://ko-fi.com/artchemylabs",
        ))

    async def _toggle_callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Settings can only be changed inside a server.", ephemeral=True
            )
            return
        if not is_moderator(interaction.user):
            await interaction.response.send_message(
                "Only moderators can change CodeClaimer settings.", ephemeral=True
            )
            return

        set_mods_only(interaction.guild.id, not get_mods_only(interaction.guild.id))
        await interaction.response.edit_message(
            embed=build_settings_embed(interaction.guild.id),
            view=SettingsView(interaction.guild.id),
        )


class BulkShareModal(discord.ui.Modal, title="Bulk Share Codes"):
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

        raw_text = str(self.batch_data.value)

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

        for item_name, platform, product_code, expires_at in valid_entries:
            await post_claim_card(
                channel=interaction.channel,
                sharer=interaction.user,
                item_name=item_name,
                platform=platform,
                product_code=product_code,
                expires_at=expires_at,
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
        super().__init__(timeout=600)

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
    mods_only = get_mods_only(guild_id)
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
        name="Expiration",
        value=(
            "Use `MM/DD/YYYY` for expiration dates. "
            "Unclaimed expired codes are automatically marked as expired."
        ),
        inline=False,
    )
    embed.add_field(name="Support", value="Use the button below to support CodeClaimer.", inline=False)
    return embed


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

class CodeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(ClaimButtonView())


bot = CodeBot()


# ---------------------------------------------------------------------------
# Expiration background task
# ---------------------------------------------------------------------------

@tasks.loop(minutes=10)
async def expire_unclaimed_codes():
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

    print(f"[Expiration] Found {len(expired)} expired code(s) to process.")

    for row in expired:
        await mark_code_expired(
            message_id=row["message_id"],
            item_name=row["item_name"],
            platform=row["platform"],
            expires_at=row["expires_at"],
            channel_id=row["channel_id"],
        )
        await asyncio.sleep(0.4)


@expire_unclaimed_codes.before_loop
async def before_expire():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user.name}")
    print(f"Database: {DB_PATH}")

    if not expire_unclaimed_codes.is_running():
        expire_unclaimed_codes.start()
        print("Expiration checker started.")

    try:
        await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as exc:
        print(f"Failed to sync commands: {exc}")


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
            "`Minecraft Skin Pack (Xbox): GHI-789 | 10/01/2026`"
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
            "Examples:\n"
            "`Hollow Knight (Steam): ABC-123 | 12/31/2026`\n"
            "`Celeste: DEF-456`"
        ),
        inline=False,
    )
    embed.add_field(
        name="/settings",
        value="Toggle Mods Only on or off. Moderator access is based on Administrator, Manage Server, or Manage Messages.",
        inline=False,
    )
    embed.add_field(
        name="Rules",
        value=(
            "No links or web addresses allowed in any field. "
            "The first person to click claims the code by DM. "
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
