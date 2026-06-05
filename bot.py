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


# List of 10 randomized title phrases (No emojis)
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


# Initialize database structure for persistence
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shared_codes (
            message_id INTEGER PRIMARY KEY,
            product_code TEXT NOT NULL,
            item_name TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT 'Unknown'
        )
    """)
    conn.commit()
    conn.close()


class ClaimButtonView(discord.ui.View):
    def __init__(self, product_code: str = None, item_name: str = None, platform: str = None):
        super().__init__(timeout=None)  # Keeps the button persistent
        self.product_code = product_code
        self.item_name = item_name
        self.platform = platform

    @discord.ui.button(label="Claim Code 🎁", style=discord.ButtonStyle.green, custom_id="claim_code_btn")
    async def claim_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = interaction.message.id

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Pull code data from SQLite if this is an old message after a reboot
        cursor.execute(
            "SELECT product_code, item_name, platform FROM shared_codes WHERE message_id = ?",
            (msg_id,)
        )
        result = cursor.fetchone()

        if result:
            code_to_send = result[0]
            item_to_send = result[1]
            platform_to_send = result[2]
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
            dm_embed.add_field(
                name="Support CodeClaimer",
                value="[Ko-Fi](<https://ko-fi.com/artchemylabs>)",
                inline=False
            )

            # DM safely first. If the DM fails, the public claim message stays available.
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

            # Grey out the original public card and remove the claim button
            await interaction.message.edit(embed=claimed_embed, view=None)

            # Clean up the database entry so it cannot be claimed again
            cursor.execute("DELETE FROM shared_codes WHERE message_id = ?", (msg_id,))
            conn.commit()

        except discord.Forbidden:
            await interaction.response.send_message(
                "Failed to send code. Please open your Privacy Settings / DMs and try again!",
                ephemeral=True
            )
        finally:
            conn.close()


class CodeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Keeps persistent claim buttons active across restarts
        self.add_view(ClaimButtonView())


bot = CodeBot()


@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user.name}!")
    try:
        await bot.tree.sync()
        print("Synced application slash commands successfully.")
    except Exception as e:
        print(f"Failed to sync application tree: {e}")


# Anti-phishing security check utility
def contains_link(text: str) -> bool:
    url_pattern = re.compile(
        r"(https?://[^\s]+)|(www\.[^\s]+)|([a-zA-Z0-9-]+\.[a-zA-Z]{2,}(/[^\s]*)?)"
    )
    return bool(url_pattern.search(text))


def parse_bulk_label(label: str):
    """
    Parses bulk item labels like:
    Game Name (Steam)

    Returns:
    item_name, platform
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


# Slash command for users to securely upload a single code
@bot.tree.command(name="sharecode", description="Share a spare product code with the community safely.")
@app_commands.describe(
    item_name="Name of the game or product",
    platform="The platform this code is for, like Steam, Epic, PS5, or Xbox",
    code="Secret activation code"
)
async def sharecode(interaction: discord.Interaction, item_name: str, platform: str, code: str):
    await interaction.response.defer(ephemeral=True)

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

    random_title = random.choice(TITLE_PHRASES)

    embed = discord.Embed(
        title=random_title,
        description=(
            f"**Product:** {item_name}\n"
            f"**Platform:** {platform}\n"
            f"**Shared by:** {interaction.user.mention}\n\n"
            f"Click the button below to claim it instantly via DM."
        ),
        color=discord.Color.gold()
    )

    view = ClaimButtonView(product_code=code, item_name=item_name, platform=platform)
    msg = await interaction.channel.send(embed=embed, view=view)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO shared_codes (message_id, product_code, item_name, platform) VALUES (?, ?, ?, ?)",
        (msg.id, code, item_name, platform)
    )
    conn.commit()
    conn.close()


# Bulk batch parser command
@bot.tree.command(
    name="bulkshare",
    description="Drop a batch of different items. Format: Game 1 (Platform) | Code1, Game 2 (Platform) | Code2"
)
@app_commands.describe(
    batch_data="Format each entry like: Game 1 (Steam) | Code1, Game 2 (Epic) | Code2"
)
async def bulkshare(interaction: discord.Interaction, batch_data: str):
    await interaction.response.defer(ephemeral=True)

    if contains_link(batch_data):
        await interaction.followup.send(
            "❌ **Submission Rejected:** Links, websites, and web addresses are strictly prohibited to prevent phishing scams.",
            ephemeral=True
        )
        return

    items = re.split(r"[\n,;]+", batch_data)
    valid_entries = []

    for item in items:
        if not item.strip():
            continue

        parts = re.split(r"[|:]", item, maxsplit=1)

        if len(parts) < 2 and " - " in item:
            parts = item.split(" - ", 1)

        if len(parts) == 2:
            raw_item_label = parts[0].strip()
            product_code = parts[1].strip()

            item_name, platform = parse_bulk_label(raw_item_label)

            if item_name and product_code:
                valid_entries.append((item_name, platform, product_code))

    if not valid_entries:
        await interaction.followup.send(
            "❌ **Format Error:** Could not parse any valid entries. Please format your list like: `Game 1 (Steam) | Code1, Game 2 (Epic) | Code2`",
            ephemeral=True
        )
        return

    await interaction.followup.send(
        f"Processing and deploying **{len(valid_entries)}** distinct claim entries...",
        ephemeral=True
    )

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    for item_name, platform, product_code in valid_entries:
        random_title = random.choice(TITLE_PHRASES)

        embed = discord.Embed(
            title=random_title,
            description=(
                f"**Product:** {item_name}\n"
                f"**Platform:** {platform}\n"
                f"**Shared by:** {interaction.user.mention}\n\n"
                f"Click the button below to claim it instantly via DM."
            ),
            color=discord.Color.gold()
        )

        view = ClaimButtonView(
            product_code=product_code,
            item_name=item_name,
            platform=platform
        )

        msg = await interaction.channel.send(embed=embed, view=view)

        cursor.execute(
            "INSERT INTO shared_codes (message_id, product_code, item_name, platform) VALUES (?, ?, ?, ?)",
            (msg.id, product_code, item_name, platform)
        )

        await asyncio.sleep(0.6)

    conn.commit()
    conn.close()


# Native Linux production boot loop
async def main():
    async with bot:
        await bot.start(BOT_TOKEN)


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("Bot turned off.")
