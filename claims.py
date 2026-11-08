"""
claims.py — Posting drops and the full claim flow.

Claim flow
  1. Member presses Claim on a card (custom_id codeclaimer_claim_code_btn,
     routed from bot.on_interaction so old and new cards share one handler).
  2. Gates: expired, role gate, cooldown, one claim per batch, wrong answer lockout.
  3. If verification is on, a private challenge appears. The gates run again
     when the member answers, so nothing can slip through while the question
     is open.
  4. db.claim_code() hands the code over in one transaction.
  5. The code is shown privately on screen (works with DMs closed). A DM copy
     is sent as a receipt when DMs are open.
  6. The public card flips to Claimed and gets a Show my code button.
"""
import logging
import time

import discord

import cards
import db
from challenges import (
    DOT_TIER_LABELS, _dot_tier_for_elapsed, generate_challenge,
)
from datetime import datetime, timezone
from parsing import clean_expiration, clean_platform, is_expired

log = logging.getLogger("codeclaimer.claims")

WRONG_ANSWER_LOCKOUT = 15          # seconds before a member can retry a card
CHALLENGE_TIMEOUT    = 60

# (message_id, user_id) -> monotonic time the lockout ends
_lockouts: dict[tuple[int, int], float] = {}
# (message_id, user_id) -> token of the newest challenge, so only the latest one counts
_active_challenges: dict[tuple[int, int], int] = {}


def is_moderator(user) -> bool:
    perms = getattr(user, "guild_permissions", None)
    if perms is None:
        return False
    return perms.administrator or perms.manage_guild or perms.manage_messages


def prune_memory() -> None:
    now = time.monotonic()
    for key, until in list(_lockouts.items()):
        if until <= now:
            _lockouts.pop(key, None)
    if len(_active_challenges) > 5000:
        _active_challenges.clear()


def format_wait(seconds: float) -> str:
    total = int(seconds + 0.999)
    mins, secs = divmod(total, 60)
    if mins and secs:
        return f"{mins}m {secs}s"
    if mins:
        return f"{mins}m"
    return f"{secs}s"


# ---------------------------------------------------------------------------
# Posting a drop
# ---------------------------------------------------------------------------

async def post_claim_card(channel, sharer, item_name: str, platform: str, product_code: str,
                          expires_at: str = "", batch_id: str = "") -> discord.Message:
    """Post a claim card and store the code. Raises discord.Forbidden if we can't post."""
    platform   = clean_platform(platform)
    expires_at = clean_expiration(expires_at)
    sharer_name = getattr(sharer, "display_name", None) or getattr(sharer, "name", "Someone")

    msg, style = await cards.send_claim_card(
        channel, item_name=item_name, platform=platform, expires_at=expires_at,
        sharer_mention=sharer.mention, sharer_name=sharer_name,
    )
    guild_id = getattr(getattr(channel, "guild", None), "id", None)
    db.insert_code(
        message_id=msg.id, product_code=product_code, item_name=item_name, platform=platform,
        expires_at=expires_at, guild_id=guild_id, channel_id=channel.id,
        sharer_id=getattr(sharer, "id", None), sharer_name=sharer_name,
        batch_id=batch_id, card_style=style,
    )
    log.info("Drop posted guild=%s channel=%s msg=%d sharer=%s batch=%s expires=%s style=%s",
             guild_id, channel.id, msg.id, getattr(sharer, "id", None),
             (batch_id or "-")[:8], "yes" if expires_at else "no", style)
    return msg


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def check_gates(row: dict, member, settings: dict) -> str | None:
    """Return a message explaining why this member can't claim right now, or None."""
    key = (row["message_id"], member.id)
    until = _lockouts.get(key, 0)
    if until > time.monotonic():
        return f"Wrong answer. You can try this one again in **{format_wait(until - time.monotonic())}**."

    roles = settings["claim_roles"]
    if roles and not is_moderator(member):
        member_roles = {r.id for r in getattr(member, "roles", [])}
        if not member_roles & set(roles):
            mentions = " ".join(f"<@&{r}>" for r in roles[:5])
            return f"Claiming here needs one of these roles: {mentions}"

    cd = settings["claim_cooldown_minutes"]
    if cd and row.get("guild_id"):
        remaining = db.cooldown_remaining(member.id, row["guild_id"], cd)
        if remaining > 0:
            return f"You're on cooldown. Try again in **{format_wait(remaining)}**."

    if row.get("batch_id") and settings["one_claim_per_batch"]:
        if db.has_claimed_from_batch(row["batch_id"], member.id):
            return "You already claimed a code from this drop. This server allows one per person."
    return None


# ---------------------------------------------------------------------------
# Claim button
# ---------------------------------------------------------------------------

async def handle_claim_click(interaction: discord.Interaction) -> None:
    msg_id = interaction.message.id
    row = db.get_code(msg_id)
    if row is None or not row.get("product_code"):
        # Already claimed or withdrawn. If this member is the one who claimed
        # it, offer the code again instead of a dead end.
        mine = db.get_claim_for_user(msg_id, interaction.user.id)
        if mine and mine.get("product_code"):
            await interaction.response.send_message(
                embed=cards.reveal_embed(mine["item_name"], mine["platform"], mine["product_code"],
                                         mine.get("expires_at") or "", note="You already claimed this one."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Someone already grabbed this one. Keep an eye out for the next drop!", ephemeral=True,
        )
        return

    if is_expired(row.get("expires_at")):
        await interaction.response.send_message("This code expired before anyone claimed it.", ephemeral=True)
        await mark_expired(row)
        return

    guild_id = interaction.guild_id
    settings = db.get_settings(guild_id) if guild_id else dict(db.DEFAULT_SETTINGS, claim_roles=[])

    blocked = check_gates(row, interaction.user, settings)
    if blocked:
        log.info("Claim blocked guild=%s msg=%d user=%d reason=%s",
                 guild_id, msg_id, interaction.user.id, blocked.split(".")[0][:40])
        await interaction.response.send_message(blocked, ephemeral=True)
        return

    if guild_id and settings["claim_verification"]:
        await _send_challenge(interaction, row, settings)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    await _complete_claim(interaction, row, settings, via_challenge=False)


async def _send_challenge(interaction: discord.Interaction, row: dict, settings: dict) -> None:
    mode = settings["verification_mode"]
    dot_start = settings["dot_start_seconds"]
    elapsed = 0.0
    if row.get("created_at"):
        try:
            elapsed = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"])).total_seconds())
        except (ValueError, TypeError):
            elapsed = 0.0

    question, answer, choices = generate_challenge(mode, elapsed, dot_start)
    key = (row["message_id"], interaction.user.id)
    token = time.monotonic_ns()
    _active_challenges[key] = token

    tier = ""
    if mode == "difficulty_over_time":
        tier = f"\n-# Difficulty right now: {DOT_TIER_LABELS.get(_dot_tier_for_elapsed(elapsed, dot_start), '')}"

    view = ChallengeView(row["message_id"], answer, choices, token)
    await interaction.response.send_message(
        f"**{question}**{tier}\n-# Answer within {CHALLENGE_TIMEOUT} seconds to claim "
        f"**{cards.item_label(row['item_name'], row.get('platform'))}**.",
        view=view, ephemeral=True,
    )


class ChallengeView(discord.ui.View):
    def __init__(self, message_id: int, answer: str, choices: list[str], token: int):
        super().__init__(timeout=CHALLENGE_TIMEOUT)
        self.message_id = message_id
        self.answer = answer
        self.token = token
        self.done = False
        for choice in choices:
            btn = discord.ui.Button(label=str(choice)[:80], style=discord.ButtonStyle.secondary)
            btn.callback = self._callback_for(str(choice))
            self.add_item(btn)

    def _callback_for(self, choice: str):
        async def callback(interaction: discord.Interaction):
            key = (self.message_id, interaction.user.id)
            if self.done:
                await interaction.response.send_message("You already answered this one.", ephemeral=True)
                return
            if _active_challenges.get(key) != self.token:
                self.done = True
                await interaction.response.edit_message(
                    content="This question was replaced by a newer one. Use the newest question.", view=None,
                )
                return
            self.done = True
            self.stop()
            _active_challenges.pop(key, None)

            if choice != self.answer:
                _lockouts[key] = time.monotonic() + WRONG_ANSWER_LOCKOUT
                log.info("Challenge wrong msg=%d user=%d", self.message_id, interaction.user.id)
                await interaction.response.edit_message(
                    content=f"❌ Not quite. You can try again in {WRONG_ANSWER_LOCKOUT} seconds.", view=None,
                )
                return

            row = db.get_code(self.message_id)
            if row is None:
                await interaction.response.edit_message(
                    content="Someone grabbed it while you were answering. Next one's yours!", view=None,
                )
                return
            guild_id = interaction.guild_id
            settings = db.get_settings(guild_id) if guild_id else dict(db.DEFAULT_SETTINGS, claim_roles=[])
            blocked = check_gates(row, interaction.user, settings)
            if blocked:
                await interaction.response.edit_message(content=blocked, view=None)
                return

            await interaction.response.defer()
            await _complete_claim(interaction, row, settings, via_challenge=True)
        return callback


# ---------------------------------------------------------------------------
# Hand over the code
# ---------------------------------------------------------------------------

async def _respond(interaction: discord.Interaction, via_challenge: bool, content=None, embed=None):
    """Answer a deferred interaction: edit the challenge message, or send the followup."""
    try:
        if via_challenge:
            await interaction.edit_original_response(content=content, embed=embed, view=None)
        else:
            await interaction.followup.send(content=content, embed=embed, ephemeral=True)
    except discord.HTTPException as e:
        log.warning("Could not answer claim interaction user=%d: %s", interaction.user.id, e)


async def _complete_claim(interaction: discord.Interaction, row: dict, settings: dict, via_challenge: bool):
    msg_id = row["message_id"]
    status, won = db.claim_code(
        msg_id, interaction.user.id, interaction.user.display_name,
        enforce_batch=bool(settings["one_claim_per_batch"]),
    )
    if status == "gone":
        await _respond(interaction, via_challenge,
                       content="Someone grabbed it a split second before you. Next one's yours!")
        return
    if status == "batch":
        await _respond(interaction, via_challenge,
                       content="You already claimed a code from this drop. This server allows one per person.")
        return

    # Private reveal first. DMs are a bonus copy, never a requirement.
    dm_ok = False
    try:
        await interaction.user.send(embed=cards.dm_receipt_embed(
            won["item_name"], won.get("platform") or "", won["product_code"], won.get("expires_at") or "",
            interaction.guild.name if interaction.guild else None, won.get("sharer_name"),
        ))
        dm_ok = True
    except (discord.Forbidden, discord.HTTPException):
        dm_ok = False

    note = ("A copy is in your DMs." if dm_ok
            else "Your DMs are closed, so save this now. The Show my code button on the card brings it back.")
    await _respond(interaction, via_challenge, embed=cards.reveal_embed(
        won["item_name"], won.get("platform") or "", won["product_code"], won.get("expires_at") or "", note=note,
    ))
    log.info("Claim guild=%s msg=%d user=%d verify=%s dm=%s",
             won.get("guild_id"), msg_id, interaction.user.id,
             settings["verification_mode"] if via_challenge else "off", "yes" if dm_ok else "no")

    await cards.update_card_claimed(
        interaction.client, won, claimer_name=interaction.user.display_name,
    )


async def handle_show_my_code(interaction: discord.Interaction) -> None:
    mine = db.get_claim_for_user(interaction.message.id, interaction.user.id)
    if not mine:
        await interaction.response.send_message("Only the person who claimed this code can see it.", ephemeral=True)
        return
    if not mine.get("product_code"):
        await interaction.response.send_message(
            "This code was claimed before CodeClaimer kept a copy. Check your DMs from CodeClaimer.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        embed=cards.reveal_embed(mine["item_name"], mine["platform"], mine["product_code"],
                                 mine.get("expires_at") or ""),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Expire, withdraw, deleted cards
# ---------------------------------------------------------------------------

async def mark_expired(row: dict) -> None:
    gone = db.delete_code(row["message_id"])
    if gone is None:
        return
    await cards.update_card_final(_bot, gone, state="expired")
    log.info("Expired msg=%d guild=%s", row["message_id"], gone.get("guild_id"))


async def withdraw(interaction: discord.Interaction, message: discord.Message) -> None:
    row = db.get_code(message.id)
    if row is None:
        await interaction.response.send_message(
            "That message isn't an unclaimed CodeClaimer drop.", ephemeral=True,
        )
        return
    is_sharer = row.get("sharer_id") == interaction.user.id
    if not (is_sharer or is_moderator(interaction.user)):
        await interaction.response.send_message(
            "Only the person who shared this code, or a moderator, can withdraw it.", ephemeral=True,
        )
        return
    gone = db.delete_code(message.id)
    if gone is None:
        await interaction.response.send_message("Too late, someone just claimed it.", ephemeral=True)
        return

    await cards.update_card_final(interaction.client, gone, state="withdrawn")
    log.info("Withdrawn msg=%d guild=%s by=%d sharer=%s",
             message.id, gone.get("guild_id"), interaction.user.id, gone.get("sharer_id"))

    if is_sharer:
        await interaction.response.send_message(
            embed=cards.reveal_embed(gone["item_name"], gone.get("platform") or "", gone["product_code"],
                                     gone.get("expires_at") or "", note="Nobody saw it. It's back in your hands.",
                                     title="↩️ Withdrawn"),
            ephemeral=True,
        )
        return
    returned = await _return_to_sharer(interaction.client, gone, reason="A moderator withdrew it")
    await interaction.response.send_message(
        "Withdrawn. " + ("The code was sent back to the sharer by DM." if returned
                         else "I couldn't DM the sharer, so the code is gone."),
        ephemeral=True,
    )


async def on_card_deleted(message_id: int) -> None:
    gone = db.delete_code(message_id)
    if gone is None:
        return
    returned = await _return_to_sharer(_bot, gone, reason="The card was deleted")
    log.info("Card deleted msg=%d guild=%s code returned=%s", message_id, gone.get("guild_id"),
             "yes" if returned else "no")


async def _return_to_sharer(client, row: dict, reason: str) -> bool:
    sharer_id = row.get("sharer_id")
    if not sharer_id:
        return False
    try:
        user = client.get_user(sharer_id) or await client.fetch_user(sharer_id)
        guild = client.get_guild(row.get("guild_id") or 0)
        where = f" in {guild.name}" if guild else ""
        await user.send(embed=cards.reveal_embed(
            row["item_name"], row.get("platform") or "", row["product_code"], row.get("expires_at") or "",
            note=f"{reason}{where} before anyone claimed it.", title="↩️ Your code is back",
        ))
        return True
    except (discord.HTTPException, discord.NotFound):
        return False


_bot = None


def bind(bot) -> None:
    global _bot
    _bot = bot
