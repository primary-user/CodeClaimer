import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import asyncio
import sqlite3
import re  # Used for anti-phishing link filtering

# ⚠️ PLACE YOUR SECURE BOT TOKEN HERE
BOT_TOKEN = "MTUxMTc1ODA4NDE5NDgzMjQ5NQ.GG0VN0.PZRmBoB_g7YJy3pg1Qwo7I5nd_LxHFoCOPnmD8"

# Initialize local database structure for persistence
def init_db():
    conn = sqlite3.connect("codes.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shared_codes (
            message_id INTEGER PRIMARY KEY,
            product_code TEXT NOT NULL,
            item_name TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

class ClaimButtonView(discord.ui.View):
    def __init__(self, product_code: str = None, item_name: str = None):
        super().__init__(timeout=None)  # Setting timeout=None makes the button persistent
        self.product_code = product_code
        self.item_name = item_name

    @discord.ui.button(label="Claim Code 🎁", style=discord.ButtonStyle.green, custom_id="claim_code_btn")
    async def claim_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = interaction.message.id
        
        # Pull code data from SQLite if it's an old message resurrected after a reboot
        conn = sqlite3.connect("codes.db")
        cursor = conn.cursor()
        cursor.execute("SELECT product_code, item_name FROM shared_codes WHERE message_id = ?", (msg_id,))
        result = cursor.fetchone()
        
        if result:
            code_to_send = result[0]
            item_to_send = result[1]
        else:
            code_to_send = self.product_code
            item_to_send = self.item_name

        if not code_to_send:
            await interaction.response.send_message("This code session expired or was already claimed!", ephemeral=True)
            conn.close()
            return

        try:
            # 1. Send the code, the item name, and a friendly community loop reminder via DM
            await interaction.user.send(
                f"Here is your claimed code for **{item_to_send}**: `{code_to_send}`\n\n"
                f"ℹ️ **Have spare keys lying around?** Help keep the cycle going! "
                f"Use the `/sharecode` command in your server to share your extra codes with the community! 🎁"
            )
            
            # 2. Confirm the claim to the user privately in the server channel
            await interaction.response.send_message(f"Success! The code has been sent to your DMs.", ephemeral=True)
            
            # 3. Clean up database entry and permanently delete the public post
            cursor.execute("DELETE FROM shared_codes WHERE message_id = ?", (msg_id,))
            conn.commit()
            await interaction.message.delete()
            
        except discord.Forbidden:
            # Safeguard: If their DMs are locked, don't drop the database record or delete the post
            await interaction.response.send_message("Failed to send code. Please open your Privacy Settings / DMs and try again!", ephemeral=True)
        finally:
            conn.close()

class CodeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Listens globally for interaction button clicks across reboots
        self.add_view(ClaimButtonView()) 

bot = CodeBot()

@bot.event
async def on_ready():
    init_db()  # Build or verify the database path immediately
    print(f'Logged in as {bot.user.name}!')
    try:
        await bot.tree.sync()
        print("Synced application slash commands successfully.")
    except Exception as e:
        print(f"Failed to sync application tree: {e}")

# Slash command for users to securely upload a code
@bot.tree.command(name="sharecode", description="Share a spare product code with the community safely.")
@app_commands.describe(item_name="Name of the game or product", code="Secret activation code")
async def sharecode(interaction: discord.Interaction, item_name: str, code: str):
    
    # 🔒 ANTI-PHISHING SECURITY FILTER
    # This regex blocks entries featuring http://, https://, www., or standard URL layouts
    url_pattern = re.compile(
        r'(https?://[^\s]+)|(www\.[^\s]+)|([a-zA-Z0-9-]+\.[a-zA-Z]{2,}(/[^\s]*)?)'
    )
    
    # Terminate routine immediately if a link footprint matches either input field
    if url_pattern.search(code) or url_pattern.search(item_name):
        await interaction.response.send_message(
            "❌ **Submission Rejected:** Links, websites, and web addresses are strictly prohibited to prevent phishing scams.", 
            ephemeral=True
        )
        return

    # Acknowledge privately so the text code never leaks into public server chat files
    await interaction.response.send_message(f"Thank you! Your code has been posted publicly.", ephemeral=True)
    
    embed = discord.Embed(
        title="🎁 Free Code Available!",
        description=f"**Product:** {item_name}\n**Shared by:** {interaction.user.mention}\n\nClick the button below to claim it instantly via DM.",
        color=discord.Color.gold()
    )
    view = ClaimButtonView(product_code=code, item_name=item_name)
    msg = await interaction.channel.send(embed=embed, view=view)
    
    # Catalog relation index points inside database 
    conn = sqlite3.connect("codes.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO shared_codes (message_id, product_code, item_name) VALUES (?, ?, ?)", (msg.id, code, item_name))
    conn.commit()
    conn.close()

# Native Linux Production Boot Loop
async def main():
    async with bot:
        await bot.start(BOT_TOKEN)

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("Bot turned off.")
