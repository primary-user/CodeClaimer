import os
import re
import random
import asyncio
import sqlite3

import discord
from discord.ext import commands
from discord import app_commands


BOT_TOKEN = os.getenv("DISCORD_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")

DB_PATH = os.getenv("DB_PATH", "data/codes.db")

db_dir = os.path.dirname(DB_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)


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
    "New Code Up For Grabs!"
]


def get_db_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shared_codes (
            message_id INTEGER PRIMARY KEY,
            product_code TEXT NOT NULL,
            item_name TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT 'Unknown'
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            mods_only INTEGER NOT NULL DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()


def contains_link(text: str) -> bool:
    url_pattern = re.compile(
        r"(https?://[^\s]+)|(www\.[^\s]+)|([a-zA-Z0-9-]+\.[a-zA-Z]{2,}(/[^\s]*)?)"
    )
    return bool(url_pattern.search(text))


def parse_bulk_label(label: str):
    """
    Parses labels like:
    Game Name (Steam)

    Returns item_name, platform.
    """
    label = label.strip()
    match = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", label)

    if match:
        item_name = match.group(1).strip()
        platform = match.group(2).strip()
    else:
        item_name = label
        platform = "Unknown"

    return item_name, platform


def split_bulk_entries(raw_text: str):
    """
    Preferred input is one code per line:
    Product Name (Platform) | Code

    Fallback separators are semicolons and commas.
    """
    if "\n" in raw_text:
        return [line.strip() for line in raw_text.splitlines() if line.strip()]

    if ";" in raw_text:
        return [entry.strip() for entry in raw_text.split(";") if entry.strip()]

    return [entry.strip() for entry in raw_text.split(",") if entry.strip()]


def is_moderator(user) -> bool:
    permissions = getattr(user, "guild_permissions", None)

    if permissions is None:
        return False

    return (
        permissions.administrator
        or permissions.manage_guild
        or permissions.manage_messages
    )


def get_mods_only(guild_id: int) -> bool:
    if guild_id is None:
        return False

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT mods_only FROM guild_settings WHERE guild_id = ?",
        (guild_id,)
    )
    result = cursor.fetchone()

    if result is None:
        cursor.execute(
            "INSERT INTO guild_settings (guild_id, mods_only) VALUES (?, 0)",
            (guild_id,)
        )
        conn.commit()
        conn.close()
        return False

    conn.close()
    return bool(result[0])


def set_mods_only(guild_id: int, enabled: bool):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO guild_settings (guild_id, mods_only)
        VALUES (?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET mods_only = excluded.mods_only
        """,
        (guild_id, 1 if enabled else 0)
    )

    conn.commit()
    conn.close()


async def user_can_use_bot(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return True

    mods_only = get_mods_only(interaction.guild.id)

    if not mods_only:
        return True

    return is_moderator(interaction.user)


async def post_claim_card(channel, sharer, item_name: str, platform: str, product_code: str):
    """
    Posts one public claim card and stores the hidden code in the persistent database.
    This is what allows claim buttons to survive bot resets, Railway redeploys, and GitHub commits.
    """
    random_title = random.choice(TITLE_PHRASES)

    embed = discord.Embed(
        title=random_title,
        description=(
            f"**Product:** {item_name}\n"
            f"**Platform:** {platform}\n"
            f"**Shared by:** {sharer.mention}\n\n"
            "Click the button below to claim it instantly via DM."
        ),
        color=discord.Color.gold()
    )

    view = ClaimButtonView(
        product_code=product_code,
        item_name=item_name,
        platform=platform
    )

    msg = await channel.send(embed=embed, view=view)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO shared_codes (message_id, product_code, item_name, platform) VALUES (?, ?, ?, ?)",
        (msg.id, product_code, item_name, platform)
    )
    conn.commit()
    conn.close()

    return msg


class ClaimButtonView(discord.ui.View):
    """
    Persistent claim button view.

    Requirements for persistence:
    - timeout=None
    - every button has a fixed custom_id
    - bot.add_view(ClaimButtonView()) is called in setup_hook
    - code data is retrieved from SQLite by message_id after restart
    """
    def __init__(self, product_code: str = None, item_name: str = None, platform: str = None):
        super().__init__(timeout=None)
        self.product_code = product_code
        self.item_name = item_name
        self.platform = platform

    @discord.ui.button(label="Claim Code 🎁", style=discord.ButtonStyle.green, custom_id="codeclaimer_claim_code_btn")
    async def claim_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = interaction.message.id

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT product_code, item_name, platform FROM shared_codes WHERE message_id = ?",
            (msg_id,)
        )
        result = cursor.fetchone()

        if result:
            code_to_send, item_to_send, platform_to_send = result
        else:
            code_to_send = self.product_code
            item_to_send = self.item_name
            platform_to_send = self.platform if self.platform else "Unknown"

        if not code_to_send:
            await interaction.response.send_message(
                "This code session expired or was already claimed!",
                ephemeral=True
            )
            conn.close()
            return

        try:
            display_platform = (
                f" ({platform_to_send})"
                if platform_to_send and platform_to_send not in ["Multi-Platform Group", ""]
                else ""
            )

            dm_embed = discord.Embed(
                title="🎁 Code Successfully Claimed!",
                description=f"Here is your activation key for **{item_to_send}**{display_platform}:",
                color=discord.Color.green()
            )
            dm_embed.add_field(name="Product Code", value=f"`{code_to_send}`", inline=False)
            dm_embed.add_field(
                name="Keep the cycle going!",
                value="Have extra keys? Use `/sharecode` to pay it forward!",
                inline=False
            )

            # DM first. If the user has DMs closed, the code stays available.
            await interaction.user.send(embed=dm_embed)

            await interaction.response.send_message(
                "Success! The code has been sent to your DMs.",
                ephemeral=True
            )

            public_embed = interaction.message.embeds[0] if interaction.message.embeds else None
            sharer_mention = "Someone"

            if public_embed and public_embed.description:
                for line in public_embed.description.split("\n"):
                    if "Shared by:" in line:
                        sharer_mention = line.replace("**Shared by:**", "").strip()
                        break

            claimed_embed = discord.Embed(
                title="Loot Claimed!",
                description=(
                    f"The code for **{item_to_send}** has been successfully claimed by "
                    f"{interaction.user.mention}!\n\n"
                    f"Thank you to {sharer_mention} for sharing with the community!"
                ),
                color=discord.Color.dark_grey()
            )

            await interaction.message.edit(embed=claimed_embed, view=None)

            cursor.execute("DELETE FROM shared_codes WHERE message_id = ?", (msg_id,))
            conn.commit()

        except discord.Forbidden:
            await interaction.response.send_message(
                "Failed to send code. Please open your Privacy Settings / DMs and try again!",
                ephemeral=True
            )
        finally:
            conn.close()


def build_settings_embed(guild_id: int):
    mods_only = get_mods_only(guild_id)

    access_mode = "Mods Only: ON" if mods_only else "Mods Only: OFF"
    access_description = (
        "Only moderators can use `/sharecode` and `/bulkshare`."
        if mods_only
        else "Members with lower roles can use `/sharecode` and `/bulkshare`."
    )

    embed = discord.Embed(
        title="CodeClaimer Settings",
        description="Manage how CodeClaimer works in this server.",
        color=discord.Color.blue()
    )

    embed.add_field(
        name="Access",
        value=(
            f"**{access_mode}**\n"
            f"{access_description}\n\n"
            "Moderator access is based on Administrator, Manage Server, or Manage Messages permissions."
        ),
        inline=False
    )

    embed.add_field(
        name="Persistence",
        value=(
            "Active claim cards and server settings are saved in SQLite. "
            "For Railway, set `DB_PATH=/data/codes.db` and use a mounted volume so data survives redeploys."
        ),
        inline=False
    )

    embed.add_field(
        name="Support",
        value="Use the button below to support CodeClaimer.",
        inline=False
    )

    return embed


class SettingsView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id

        mods_only = get_mods_only(guild_id)

        toggle_button = discord.ui.Button(
            label="Mods Only: ON" if mods_only else "Mods Only: OFF",
            style=discord.ButtonStyle.danger if mods_only else discord.ButtonStyle.success,
            custom_id="codeclaimer_toggle_mods_only"
        )
        toggle_button.callback = self.toggle_mods_only_callback
        self.add_item(toggle_button)

        self.add_item(
            discord.ui.Button(
                label="Support CodeClaimer",
                style=discord.ButtonStyle.link,
                url="https://ko-fi.com/artchemylabs"
            )
        )

    async def toggle_mods_only_callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Settings can only be changed inside a server.",
                ephemeral=True
            )
            return

        if not is_moderator(interaction.user):
            await interaction.response.send_message(
                "Only moderators can change CodeClaimer settings.",
                ephemeral=True
            )
            return

        current = get_mods_only(interaction.guild.id)
        set_mods_only(interaction.guild.id, not current)

        updated_embed = build_settings_embed(interaction.guild.id)
        updated_view = SettingsView(interaction.guild.id)

        await interaction.response.edit_message(
            embed=updated_embed,
            view=updated_view
        )


class BulkShareModal(discord.ui.Modal, title="Bulk Share Codes"):
    batch_data = discord.ui.TextInput(
        label="Paste codes, one per line",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000,
        placeholder=(
            "Hollow Knight (Steam) | ABC-123\n"
            "Celeste (Epic) | DEF-456\n"
            "Minecraft Skin Pack (Xbox) | GHI-789"
        )
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not await user_can_use_bot(interaction):
            await interaction.followup.send(
                "CodeClaimer is currently set to **Mods Only: ON**. Ask a moderator to share these codes.",
                ephemeral=True
            )
            return

        raw_text = str(self.batch_data.value)

        if contains_link(raw_text):
            await interaction.followup.send(
                "❌ **Submission Rejected:** Links, websites, and web addresses are strictly prohibited to prevent phishing scams.",
                ephemeral=True
            )
            return

        items = split_bulk_entries(raw_text)
        valid_entries = []
        skipped_entries = []

        for item in items:
            parts = re.split(r"[|:]", item, maxsplit=1)

            if len(parts) < 2 and " - " in item:
                parts = item.split(" - ", 1)

            if len(parts) == 2:
                raw_item_label = parts[0].strip()
                product_code = parts[1].strip()

                item_name, platform = parse_bulk_label(raw_item_label)

                if item_name and product_code:
                    valid_entries.append((item_name, platform, product_code))
                else:
                    skipped_entries.append(item)
            else:
                skipped_entries.append(item)

        if not valid_entries:
            await interaction.followup.send(
                (
                    "❌ **Format Error:** Could not parse any valid entries.\n\n"
                    "Use one code per line in this format:\n"
                    "`Product Name (Platform) | Code`\n\n"
                    "Example:\n"
                    "`Hollow Knight (Steam) | ABC-123`\n"
                    "`Celeste (Epic) | DEF-456`"
                ),
                ephemeral=True
            )
            return

        await interaction.followup.send(
            f"Processing **{len(valid_entries)}** claim entries...",
            ephemeral=True
        )

        for item_name, platform, product_code in valid_entries:
            await post_claim_card(
                channel=interaction.channel,
                sharer=interaction.user,
                item_name=item_name,
                platform=platform,
                product_code=product_code
            )
            await asyncio.sleep(0.6)

        if skipped_entries:
            skipped_preview = "\n".join(f"- {entry}" for entry in skipped_entries[:5])
            more_text = ""
            if len(skipped_entries) > 5:
                more_text = f"\n...and {len(skipped_entries) - 5} more."

            await interaction.followup.send(
                (
                    f"Done. Posted **{len(valid_entries)}** claim entries.\n\n"
                    f"Skipped **{len(skipped_entries)}** entries because they did not match the required format:\n"
                    f"{skipped_preview}{more_text}"
                ),
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Done. Posted **{len(valid_entries)}** claim entries.",
                ephemeral=True
            )


class BulkSharePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Open Bulk Entry Form", style=discord.ButtonStyle.primary, custom_id="codeclaimer_open_bulk_modal")
    async def open_bulk_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await user_can_use_bot(interaction):
            await interaction.response.send_message(
                "CodeClaimer is currently set to **Mods Only: ON**. Ask a moderator to share these codes.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(BulkShareModal())


class CodeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # This line is what re-registers old claim buttons after every restart, Railway redeploy, or GitHub commit.
        self.add_view(ClaimButtonView())


bot = CodeBot()


@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user.name}!")
    print(f"Using SQLite database at: {DB_PATH}")
    try:
        await bot.tree.sync()
        print("Synced application slash commands successfully.")
    except Exception as e:
        print(f"Failed to sync application tree: {e}")


@bot.tree.command(name="help", description="Show CodeClaimer instructions.")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="CodeClaimer Help",
        description=(
            "CodeClaimer lets your community safely share spare product, game, or access codes. "
            "Codes are hidden publicly and sent by DM to the first person who claims them."
        ),
        color=discord.Color.blue()
    )

    embed.add_field(
        name="/sharecode",
        value=(
            "Use this to share one code.\n\n"
            "**Fields:**\n"
            "`item_name` - Name of the game or product\n"
            "`platform` - Platform, such as Steam, Epic, PS5, or Xbox\n"
            "`code` - The private code\n\n"
            "**Example:**\n"
            "`/sharecode item_name: Hollow Knight platform: Steam code: ABC-123`"
        ),
        inline=False
    )

    embed.add_field(
        name="/bulkshare",
        value=(
            "Use this to share multiple codes. The command opens a private instruction panel. "
            "Click **Open Bulk Entry Form**, then paste multiple lines.\n\n"
            "**Preferred format, one code per line:**\n"
            "`Product Name (Platform) | Code`\n\n"
            "**Example:**\n"
            "`Hollow Knight (Steam) | ABC-123`\n"
            "`Celeste (Epic) | DEF-456`\n"
            "`Minecraft Skin Pack (Xbox) | GHI-789`\n\n"
            "Commas and semicolons are accepted as fallbacks, but line breaks are recommended."
        ),
        inline=False
    )

    embed.add_field(
        name="/settings",
        value="Opens the settings panel with the Mods Only toggle and support button.",
        inline=False
    )

    embed.add_field(
        name="Rules",
        value=(
            "No links, websites, or web addresses are allowed. "
            "The first person to claim receives the code by DM. "
            "After claiming, the public post is marked as claimed and the button is removed."
        ),
        inline=False
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="settings", description="Open CodeClaimer settings.")
async def settings_command(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Settings can only be opened inside a server.",
            ephemeral=True
        )
        return

    embed = build_settings_embed(interaction.guild.id)
    view = SettingsView(interaction.guild.id)

    await interaction.response.send_message(
        embed=embed,
        view=view,
        ephemeral=True
    )


@bot.tree.command(name="sharecode", description="Share a spare product code with the community safely.")
@app_commands.describe(
    item_name="Name of the game or product",
    platform="The platform this code is for, like Steam, Epic, PS5, or Xbox",
    code="Secret activation code"
)
async def sharecode(interaction: discord.Interaction, item_name: str, platform: str, code: str):
    await interaction.response.defer(ephemeral=True)

    if not await user_can_use_bot(interaction):
        await interaction.followup.send(
            "CodeClaimer is currently set to **Mods Only: ON**. Ask a moderator to share this code.",
            ephemeral=True
        )
        return

    if contains_link(code) or contains_link(item_name) or contains_link(platform):
        await interaction.followup.send(
            "❌ **Submission Rejected:** Links, websites, and web addresses are strictly prohibited to prevent phishing scams.",
            ephemeral=True
        )
        return

    await interaction.followup.send(
        "Thank you! Your code has been posted publicly.",
        ephemeral=True
    )

    await post_claim_card(
        channel=interaction.channel,
        sharer=interaction.user,
        item_name=item_name,
        platform=platform,
        product_code=code
    )


@bot.tree.command(name="bulkshare", description="Open a guided panel to share multiple codes at once.")
async def bulkshare(interaction: discord.Interaction):
    if not await user_can_use_bot(interaction):
        await interaction.response.send_message(
            "CodeClaimer is currently set to **Mods Only: ON**. Ask a moderator to share these codes.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="Bulk Share Codes",
        description=(
            "Paste multiple codes into a private form. Each code should be on its own line.\n\n"
            "**Required format:**\n"
            "`Product Name (Platform) | Code`\n\n"
            "**Example:**\n"
            "`Hollow Knight (Steam) | ABC-123`\n"
            "`Celeste (Epic) | DEF-456`\n"
            "`Minecraft Skin Pack (Xbox) | GHI-789`\n\n"
            "Line breaks are recommended. Commas and semicolons are accepted as fallbacks."
        ),
        color=discord.Color.blue()
    )

    await interaction.response.send_message(
        embed=embed,
        view=BulkSharePanelView(),
        ephemeral=True
    )


async def main():
    async with bot:
        await bot.start(BOT_TOKEN)


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("Bot turned off.")
