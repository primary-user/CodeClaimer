"""
cards.py — Everything a member sees: claim cards, claimed and expired states,
the private code reveal, and the DM receipt.

Two card styles live side by side:
  legacy  Embed + button cards posted before the v2 upgrade. They keep their
          look and their button, and are updated with embeds.
  v2      Components V2 cards (added in batch 2).
"""
import random

import discord

from parsing import clean_expiration, clean_platform, expiry_timestamp

BOT_ID      = 1511758084194832495
TOPGG_URL   = f"https://top.gg/bot/{BOT_ID}"
SUPPORT_URL = "https://discord.gg/NSt5Rcm8VN"
KOFI_URL    = "https://ko-fi.com/artchemylabs"

# Brand, from the claw icon
SKY   = 0x3EAFD4
NAVY  = 0x0A428E
SLATE = 0x4E5058

CLAIM_CUSTOM_ID   = "codeclaimer_claim_code_btn"   # never change: every live card uses it
MYCODE_CUSTOM_ID  = "cc:mycode"

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


def item_label(item_name: str, platform: str | None) -> str:
    p = clean_platform(platform)
    return f"{item_name} ({p})" if p else item_name


def expiry_text(expires_at: str | None) -> str:
    """Discord timestamp when the date parses, else the raw text."""
    ts = expiry_timestamp(expires_at)
    if ts:
        return f"<t:{ts}:f> (<t:{ts}:R>)"
    return clean_expiration(expires_at)


# ---------------------------------------------------------------------------
# Legacy embed cards
# ---------------------------------------------------------------------------

def legacy_card_embed(item_name: str, platform: str, expires_at: str, sharer_mention: str) -> discord.Embed:
    lines = [f"**Product:** {item_name}"]
    if clean_platform(platform):
        lines.append(f"**Platform:** {clean_platform(platform)}")
    if clean_expiration(expires_at):
        lines.append(f"**Expires:** {expiry_text(expires_at)}")
    lines.append(f"**Shared by:** {sharer_mention}")
    lines.append("")
    lines.append("Press the button to claim it. Your code shows up right here, only to you.")
    return discord.Embed(title=random.choice(TITLE_PHRASES), description="\n".join(lines), color=SKY)


def legacy_claim_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="Claim Code 🎁", style=discord.ButtonStyle.green, custom_id=CLAIM_CUSTOM_ID,
    ))
    return view


def legacy_claimed_embed(item_name: str, platform: str, claimer: str, sharer: str) -> discord.Embed:
    return discord.Embed(
        title="Loot Claimed!",
        description=(
            f"The code for **{item_label(item_name, platform)}** was claimed by **{claimer}**.\n\n"
            f"Thanks to {sharer} for sharing with the community!"
        ),
        color=discord.Color.dark_grey(),
    )


def legacy_claimed_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="Show my code", style=discord.ButtonStyle.secondary, custom_id=MYCODE_CUSTOM_ID,
    ))
    return view


def legacy_expired_embed(item_name: str, platform: str, expires_at: str) -> discord.Embed:
    return discord.Embed(
        title="Code Expired",
        description=(
            f"The code for **{item_label(item_name, platform)}** expired before anyone claimed it.\n\n"
            f"**Expired:** {expiry_text(expires_at)}"
        ),
        color=discord.Color.dark_grey(),
    )


def legacy_withdrawn_embed(item_name: str, platform: str) -> discord.Embed:
    return discord.Embed(
        title="Code Withdrawn",
        description=f"The code for **{item_label(item_name, platform)}** was taken back before anyone claimed it.",
        color=discord.Color.dark_grey(),
    )


# ---------------------------------------------------------------------------
# Private reveal and DM receipt
# ---------------------------------------------------------------------------

def reveal_embed(item_name: str, platform: str, code: str, expires_at: str, note: str = "",
                 title: str = "🎁 It's yours") -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=f"**{item_label(item_name, platform)}**\n```\n{code}\n```",
        color=NAVY,
    )
    if clean_expiration(expires_at):
        embed.add_field(name="Redeem before", value=expiry_text(expires_at), inline=False)
    if note:
        embed.set_footer(text=note)
    return embed


def dm_receipt_embed(item_name: str, platform: str, code: str, expires_at: str,
                     guild_name: str | None, sharer: str | None) -> discord.Embed:
    where = f" in **{guild_name}**" if guild_name else ""
    by = f" Shared by {sharer}." if sharer else ""
    embed = discord.Embed(
        title="🎁 Your claimed code",
        description=f"**{item_label(item_name, platform)}**{where}.{by}\n```\n{code}\n```",
        color=NAVY,
    )
    if clean_expiration(expires_at):
        embed.add_field(name="Redeem before", value=expiry_text(expires_at), inline=False)
    embed.add_field(
        name="Keep the cycle going",
        value=f"Want code drops in your own server? [Add CodeClaimer or vote on Top.gg]({TOPGG_URL})",
        inline=False,
    )
    return embed


# ---------------------------------------------------------------------------
# Send and update cards
# ---------------------------------------------------------------------------

async def send_claim_card(channel, item_name: str, platform: str, expires_at: str,
                          sharer_mention: str, sharer_name: str) -> tuple[discord.Message, str]:
    """Post a new claim card. Returns (message, card_style)."""
    msg = await channel.send(
        embed=legacy_card_embed(item_name, platform, expires_at, sharer_mention),
        view=legacy_claim_view(),
    )
    return msg, "legacy"


def _sharer_display(row: dict) -> str:
    name = row.get("sharer_name")
    if name and name != "Someone":
        return name
    return f"<@{row['sharer_id']}>" if row.get("sharer_id") else "someone"


async def _card_message(client, row: dict):
    channel_id = row.get("channel_id")
    if not channel_id:
        return None
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel.get_partial_message(row["message_id"])


async def _edit_card(client, row: dict, **kwargs) -> bool:
    msg = await _card_message(client, row)
    if msg is None:
        return False
    try:
        await msg.edit(**kwargs)
        return True
    except discord.HTTPException as e:
        import logging
        logging.getLogger("codeclaimer.cards").info(
            "Card edit failed msg=%d: %s", row["message_id"], e)
        return False


async def update_card_claimed(client, row: dict, claimer_name: str) -> None:
    await _edit_card(
        client, row,
        embed=legacy_claimed_embed(row["item_name"], row.get("platform") or "", claimer_name, _sharer_display(row)),
        view=legacy_claimed_view(),
    )


async def update_card_final(client, row: dict, state: str) -> None:
    """state: expired | withdrawn. Removes the button."""
    if state == "expired":
        embed = legacy_expired_embed(row["item_name"], row.get("platform") or "", row.get("expires_at") or "")
    else:
        embed = legacy_withdrawn_embed(row["item_name"], row.get("platform") or "")
    await _edit_card(client, row, embed=embed, view=None)
