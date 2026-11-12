"""
cards.py — Everything a member sees: claim cards, claimed / expired / withdrawn
states, the private code reveal, and the DM receipt.

Two card styles live side by side:
  legacy  Embed + button cards posted before v2. Their Claim button keeps
          working. When one is claimed, expires or is withdrawn we try to
          redraw it as a v2 card, and fall back to an embed if Discord says no.
  v2      Components V2 cards. Swiss layout: small uppercase label, big item
          name, one fact line, buttons. Accent bar in the claw icon's blues.

Every card ends with a small Add CodeClaimer link and an animated claw tile
for its state (live, claimed, empty).
"""
import logging
import os
import random
from datetime import datetime, timezone

import discord
from discord import ui

from parsing import clean_expiration, clean_platform, expiry_timestamp

log = logging.getLogger("codeclaimer.cards")

BOT_ID      = 1511758084194832495
TOPGG_URL   = f"https://top.gg/bot/{BOT_ID}"
VOTE_URL    = f"https://top.gg/bot/{BOT_ID}/vote"
SUPPORT_URL = "https://discord.gg/NSt5Rcm8VN"
KOFI_URL    = "https://ko-fi.com/artchemylabs"
# View Channel, Send Messages, Embed Links, Read Message History
INVITE_URL  = (f"https://discord.com/oauth2/authorize?client_id={BOT_ID}"
               "&permissions=84992&integration_type=0&scope=bot+applications.commands")

# Brand, from the claw icon
SKY   = 0x3EAFD4
NAVY  = 0x0A428E
SLATE = 0x4E5058

CLAIM_CUSTOM_ID  = "codeclaimer_claim_code_btn"   # never change: every live card uses it
MYCODE_CUSTOM_ID = "cc:mycode"

TITLE_PHRASES = [
    "Free Code Available!", "Loot Drop Alert!", "A New Key Arrives!", "Claim This Code!",
    "Fresh Drop in the Channel!", "Spare Code Detected!", "Grab It While It's Hot!",
    "Community Gift Available!", "First Come First Served!", "New Code Up For Grabs!",
]

# Small uppercase labels for the top of a live v2 card
DROP_LABELS = [
    "CODE DROP", "FRESH DROP", "SPARE KEY", "COMMUNITY GIFT", "UP FOR GRABS", "LOOT DROP",
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


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# Claw tiles
# ---------------------------------------------------------------------------
# Animated thumbnails built by tools/build_claw.py from the brand icon. They are
# served straight from the public GitHub repo, so servers need no Attach Files
# permission and nothing is uploaded per card. Discord's media proxy caches them.
# New art gets a new file name (v2, v3...) so cached copies never go stale.
# CLAW_ASSET_BASE points somewhere else. CLAW_TILES=0 turns them off.

ASSET_BASE = os.getenv("CLAW_ASSET_BASE",
                       "https://raw.githubusercontent.com/primary-user/CodeClaimer/main/assets").rstrip("/")
TILES_ON = os.getenv("CLAW_TILES", "1") != "0"
TILE_LIVE    = f"{ASSET_BASE}/claw-live-v1.gif"
TILE_CLAIMED = f"{ASSET_BASE}/claw-claimed-v1.gif"
TILE_EMPTY   = f"{ASSET_BASE}/claw-empty-v1.png"

INVITE_LINE = f"-# [Add CodeClaimer to your server]({INVITE_URL})"


def invite_button(label: str = "Add to your server") -> ui.Button:
    """Big link button. Only used on /help; cards use INVITE_LINE instead."""
    return ui.Button(label=label, style=discord.ButtonStyle.link, url=INVITE_URL)


# Claw art rides inline as an app emoji beside the title. Discord gives no size
# control over a Section thumbnail, and it made cards tall on mobile.
_EMOJI_FILES = {
    TILE_LIVE:    ("claw_live_v1",    "claw-live-v1.gif"),
    TILE_CLAIMED: ("claw_claimed_v1", "claw-claimed-v1.gif"),
    TILE_EMPTY:   ("claw_empty_v1",   "claw-empty-v1.png"),
}
TILE_EMOJI: dict[str, str] = {}


async def ensure_emojis(client) -> None:
    """Upload the claw tiles as app emojis once, then remember them. Fail safe:
    without them, cards simply show no claw."""
    if not TILES_ON:
        return
    try:
        have = {e.name: e for e in await client.fetch_application_emojis()}
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
        for tile, (name, fname) in _EMOJI_FILES.items():
            e = have.get(name)
            if e is None:
                with open(os.path.join(folder, fname), "rb") as f:
                    e = await client.create_application_emoji(name=name, image=f.read())
                log.info("Uploaded app emoji %s", name)
            TILE_EMOJI[tile] = str(e)
        log.info("Claw emojis ready: %d", len(TILE_EMOJI))
    except Exception:
        log.exception("Claw emojis unavailable, cards will show without the claw")


def _compact_title(text: str, emoji: str) -> str:
    """Title one size down (## to ###) with the claw emoji in front of it."""
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith("## ") or ln.startswith("### "):
            title = ln.split(" ", 1)[1]
            lines[i] = f"### {emoji} {title}" if emoji else f"### {title}"
            break
    return "\n".join(lines)


def tile_section(texts: list[str], tile: str, alt: str):
    """Card text with a small claw emoji beside the title."""
    emoji = TILE_EMOJI.get(tile, "") if TILES_ON else ""
    return [ui.TextDisplay(_compact_title(t, emoji)) for t in texts[:3]]


# ---------------------------------------------------------------------------
# v2 cards
# ---------------------------------------------------------------------------

def _facts(platform: str, expires_at: str, extra: list[str] | None = None) -> str:
    bits = []
    if clean_platform(platform):
        bits.append(f"**{clean_platform(platform)}**")
    ts = expiry_timestamp(expires_at)
    if ts:
        bits.append(f"Expires <t:{ts}:R>")
    elif clean_expiration(expires_at):
        bits.append(f"Expires {clean_expiration(expires_at)}")
    bits.extend(extra or [])
    return "  ·  ".join(bits)


def v2_live_card(item_name: str, platform: str, expires_at: str, sharer_mention: str,
                 label: str | None = None, challenge: bool = False, note: str = "") -> ui.LayoutView:
    view = ui.LayoutView(timeout=None)
    how = "Solve a quick question to claim" if challenge else "First to claim gets it"
    head = f"-# {label or random.choice(DROP_LABELS)}  ·  {how.upper()}"
    body = f"## {item_name}"
    facts = _facts(platform, expires_at)
    if facts:
        body += f"\n{facts}"
    if note:
        body += f"\n{note}"
    view.add_item(ui.Container(
        *tile_section([head, body], TILE_LIVE, "Claw reaching for a gift"),
        ui.TextDisplay(f"-# Shared by {sharer_mention}. The code only shows to whoever claims it."),
        ui.Separator(),
        ui.ActionRow(
            ui.Button(label="Claim code", style=discord.ButtonStyle.success, custom_id=CLAIM_CUSTOM_ID,
                      emoji="🎁"),
        ),
        ui.TextDisplay(INVITE_LINE),
        accent_color=SKY,
    ))
    return view


def v2_claimed_card(item_name: str, platform: str, claimer: str, sharer: str,
                    claimed_ts: int | None = None) -> ui.LayoutView:
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(
        *tile_section([
            "-# CLAIMED",
            f"### {item_label(item_name, platform)}\n"
            f"Claimed by **{claimer}** <t:{claimed_ts or _now_ts()}:R>. Thanks to **{sharer}** for sharing.",
        ], TILE_CLAIMED, "Claw took the gift"),
        ui.Separator(),
        ui.ActionRow(
            ui.Button(label="Show my code", style=discord.ButtonStyle.secondary, custom_id=MYCODE_CUSTOM_ID),
        ),
        ui.TextDisplay(INVITE_LINE),
        accent_color=NAVY,
    ))
    return view


def v2_final_card(item_name: str, platform: str, state: str, expires_at: str = "") -> ui.LayoutView:
    if state == "expired":
        ts = expiry_timestamp(expires_at)
        when = f" <t:{ts}:R>" if ts else ""
        label, line = "EXPIRED", f"Nobody claimed it before it expired{when}."
    else:
        label, line = "WITHDRAWN", "The sharer took this code back."
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(
        *tile_section([f"-# {label}", f"### {item_label(item_name, platform)}\n{line}"],
                      TILE_EMPTY, "Empty claw"),
        ui.Separator(),
        ui.TextDisplay(INVITE_LINE),
        accent_color=SLATE,
    ))
    return view


# ---------------------------------------------------------------------------
# Legacy embed states (fallback for cards posted before v2)
# ---------------------------------------------------------------------------

def _legacy_tile(embed: discord.Embed, tile: str) -> discord.Embed:
    if TILES_ON:
        embed.set_thumbnail(url=tile)
    return embed


def legacy_claimed_embed(item_name: str, platform: str, claimer: str, sharer: str) -> discord.Embed:
    return _legacy_tile(discord.Embed(
        title="Loot Claimed!",
        description=(f"The code for **{item_label(item_name, platform)}** was claimed by **{claimer}**.\n\n"
                     f"Thanks to {sharer} for sharing with the community!\n{INVITE_LINE}"),
        color=NAVY,
    ), TILE_CLAIMED)


def legacy_claimed_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Show my code", style=discord.ButtonStyle.secondary,
                                    custom_id=MYCODE_CUSTOM_ID))
    return view


def legacy_final_embed(item_name: str, platform: str, state: str, expires_at: str = "") -> discord.Embed:
    if state == "expired":
        return _legacy_tile(discord.Embed(
            title="Code Expired",
            description=(f"The code for **{item_label(item_name, platform)}** expired before anyone claimed it.\n\n"
                         f"**Expired:** {expiry_text(expires_at)}\n{INVITE_LINE}"),
            color=SLATE,
        ), TILE_EMPTY)
    return _legacy_tile(discord.Embed(
        title="Code Withdrawn",
        description=(f"The code for **{item_label(item_name, platform)}** was taken back before anyone "
                     f"claimed it.\n{INVITE_LINE}"),
        color=SLATE,
    ), TILE_EMPTY)


def legacy_link_view() -> None:
    """Final legacy states carry no buttons; the invite is a text line."""
    return None


# ---------------------------------------------------------------------------
# Private reveal and DM receipt
# ---------------------------------------------------------------------------

MOBILE_TIP = "On mobile, long-press the code above and tap Copy Text."
NO_MENTIONS = discord.AllowedMentions.none()


def code_content(code: str) -> str:
    """The bare code alone as message text. Mobile can't copy from embeds, so with the
    code as the only text, long press Copy Text grabs it. No code block: mobile copies
    the backticks too (tested Nov 11)."""
    return str(code).strip()[:1900]


def reveal_embed(item_name: str, platform: str, code: str, expires_at: str, note: str = "",
                 title: str = "🎁 It's yours", tip: str = MOBILE_TIP) -> discord.Embed:
    """Details card for a private reveal. The code itself goes in the message content (see reveal_kwargs)."""
    embed = discord.Embed(title=title, description=f"**{item_label(item_name, platform)}**", color=NAVY)
    if clean_expiration(expires_at):
        embed.add_field(name="Redeem before", value=expiry_text(expires_at), inline=False)
    embed.set_footer(text=f"{note} {tip}".strip())
    return embed


def reveal_kwargs(item_name: str, platform: str, code: str, expires_at: str, note: str = "",
                  title: str = "🎁 It's yours") -> dict:
    """Message kwargs: bare code as content (copyable on mobile), details card underneath."""
    return dict(content=code_content(code), allowed_mentions=NO_MENTIONS,
                embed=reveal_embed(item_name, platform, code, expires_at, note=note, title=title))


def dm_receipt_embed(item_name: str, platform: str, code: str, expires_at: str,
                     guild_name: str | None, sharer: str | None, raffle: bool = False) -> discord.Embed:
    where = f" in **{guild_name}**" if guild_name else ""
    by = f" Shared by {sharer}." if sharer else ""
    how = "You won this in a raffle" if raffle else "You claimed this"
    desc = f"{how}{where}.{by}\n\n**{item_label(item_name, platform)}**"
    if clean_expiration(expires_at):
        desc += f"\n**Redeem before** {expiry_text(expires_at)}"
    desc += f"\n\n{MOBILE_TIP}"
    desc += f"\n-# [Add CodeClaimer to your server]({INVITE_URL})  ·  [Vote on Top.gg]({VOTE_URL})"
    embed = discord.Embed(title="🎁 Your code", description=desc, color=NAVY)
    return _legacy_tile(embed, TILE_CLAIMED)


async def send_dm_code(user, embed: discord.Embed, code: str) -> None:
    """One DM: the code above, the details card below. Raises if DMs are closed."""
    await user.send(content=code_content(code), embed=embed, allowed_mentions=NO_MENTIONS)


# ---------------------------------------------------------------------------
# Send and update cards
# ---------------------------------------------------------------------------

async def send_claim_card(channel, item_name: str, platform: str, expires_at: str,
                          sharer_mention: str, sharer_name: str, challenge: bool = False,
                          label: str | None = None, note: str = "") -> tuple[discord.Message, str]:
    """Post a new claim card. Returns (message, card_style)."""
    msg = await channel.send(
        view=v2_live_card(item_name, platform, expires_at, sharer_mention, label=label, challenge=challenge,
                          note=note),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    return msg, "v2"


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


# Flips to False the first time Discord refuses to turn an old embed card
# into a v2 card, so we stop trying and use embeds for the rest of the run.
_legacy_upgrade_ok = True


async def _edit_card(client, row: dict, v2_view: ui.LayoutView, legacy_embed: discord.Embed,
                     legacy_view: discord.ui.View | None) -> bool:
    global _legacy_upgrade_ok
    msg = await _card_message(client, row)
    if msg is None:
        return False
    style = row.get("card_style") or "legacy"
    try:
        if style == "v2":
            await msg.edit(view=v2_view, allowed_mentions=discord.AllowedMentions.none())
            return True
        if _legacy_upgrade_ok:
            try:
                await msg.edit(content=None, embed=None, view=v2_view,
                               allowed_mentions=discord.AllowedMentions.none())
                return True
            except discord.HTTPException as e:
                if e.status in (400, 403) and getattr(e, "code", 0) != 50001:
                    _legacy_upgrade_ok = False
                    log.info("Old cards can't be redrawn as v2 (%s). Using embeds for old cards.", e)
                else:
                    raise
        await msg.edit(embed=legacy_embed, view=legacy_view)
        return True
    except discord.HTTPException as e:
        log.info("Card edit failed msg=%d: %s", row["message_id"], e)
        return False


async def update_card_claimed(client, row: dict, claimer_name: str) -> None:
    item, plat, sharer = row["item_name"], row.get("platform") or "", _sharer_display(row)
    await _edit_card(client, row,
                     v2_claimed_card(item, plat, claimer_name, sharer),
                     legacy_claimed_embed(item, plat, claimer_name, sharer),
                     legacy_claimed_view())


async def update_card_final(client, row: dict, state: str) -> None:
    """state: expired | withdrawn. No claim button afterwards."""
    item, plat, exp = row["item_name"], row.get("platform") or "", row.get("expires_at") or ""
    await _edit_card(client, row,
                     v2_final_card(item, plat, state, exp),
                     legacy_final_embed(item, plat, state, exp),
                     legacy_link_view())
