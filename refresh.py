"""
refresh.py — One time redraw of every card posted before the v2 look.

On startup a background task walks:
  1. live, unclaimed codes still on old embed cards  -> v2 live card + live tile
  2. every claimed drop in claims_log                  -> v2 claimed card + claimed tile
Only servers the bot is still in are touched. Biggest servers go first.

Each card is recorded in card_refresh with its result, so a restart carries on
where it stopped and a card is never edited twice. The Claim button keeps its
custom_id, so refreshed live cards claim exactly as before. Codes are never
changed or deleted here.

Results: ok | missing (message deleted) | no_access (channel hidden) |
         error (anything else, logged). CARD_REFRESH=0 skips the job.
"""
import asyncio
import logging
import os
from datetime import datetime

import discord

import cards
import db
from parsing import is_expired

log = logging.getLogger("codeclaimer.refresh")

PACE_SECONDS = 1.0
_running = False


def init_table() -> None:
    with db.get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS card_refresh (
                message_id INTEGER PRIMARY KEY,
                kind       TEXT NOT NULL,
                result     TEXT NOT NULL,
                done_at    TEXT NOT NULL
            )
        """)
        conn.commit()


def _record(message_id: int, kind: str, result: str) -> None:
    with db.get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO card_refresh (message_id, kind, result, done_at) VALUES (?, ?, ?, ?)",
                     (message_id, kind, result, db.now_iso()))
        conn.commit()


def pending(guild_ids: set[int], member_counts: dict[int, int]) -> list[tuple[str, dict]]:
    """Cards still to redraw, as (kind, row), biggest servers first."""
    with db.get_db() as conn:
        live = [dict(r) for r in conn.execute(
            """SELECT * FROM shared_codes s
               WHERE card_style = 'legacy' AND guild_id IS NOT NULL AND channel_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM card_refresh c WHERE c.message_id = s.message_id)""").fetchall()]
        claimed = [dict(r) for r in conn.execute(
            """SELECT l.* FROM claims_log l
               WHERE l.source = 'drop' AND l.message_id IS NOT NULL AND l.channel_id IS NOT NULL
                 AND l.id = (SELECT MAX(id) FROM claims_log x WHERE x.message_id = l.message_id)
                 AND NOT EXISTS (SELECT 1 FROM card_refresh c WHERE c.message_id = l.message_id)""").fetchall()]
    jobs = [("live", r) for r in live if r["guild_id"] in guild_ids and not is_expired(r.get("expires_at"))]
    jobs += [("claimed", r) for r in claimed if r["guild_id"] in guild_ids]
    jobs.sort(key=lambda j: (-member_counts.get(j[1]["guild_id"], 0), j[0] != "live", j[1]["message_id"]))
    return jobs


def _ts(iso: str | None) -> int | None:
    try:
        return int(datetime.fromisoformat(iso).timestamp()) if iso else None
    except (ValueError, TypeError):
        return None


def _view_for(kind: str, row: dict, client) -> discord.ui.LayoutView:
    if kind == "live":
        sharer = f"<@{row['sharer_id']}>" if row.get("sharer_id") else (row.get("sharer_name") or "someone")
        challenge = bool(db.get_settings(row["guild_id"])["claim_verification"])
        return cards.v2_live_card(row["item_name"], row.get("platform") or "", row.get("expires_at") or "",
                                  sharer, challenge=challenge)
    sharer = row.get("sharer_name") or (f"<@{row['sharer_id']}>" if row.get("sharer_id") else "someone")
    return cards.v2_claimed_card(row["item_name"], row.get("platform") or "", row.get("claimer_name") or "someone",
                                 sharer, claimed_ts=_ts(row.get("claimed_at")))


async def _redraw(client, kind: str, row: dict) -> str:
    # A live code claimed while we were waiting is no longer live: skip it,
    # the claim already redrew the card.
    if kind == "live" and db.get_code(row["message_id"]) is None:
        return "skipped"
    msg = await cards._card_message(client, row)
    if msg is None:
        return "no_access"
    try:
        await msg.edit(content=None, embed=None, view=_view_for(kind, row, client),
                       allowed_mentions=discord.AllowedMentions.none())
    except discord.NotFound:
        return "missing"
    except discord.Forbidden:
        return "no_access"
    except discord.HTTPException as e:
        log.warning("Refresh edit failed msg=%d kind=%s: %s", row["message_id"], kind, e)
        return "error"
    if kind == "live":
        with db.get_db() as conn:
            conn.execute("UPDATE shared_codes SET card_style = 'v2' WHERE message_id = ?", (row["message_id"],))
            conn.commit()
    return "ok"


async def run(client) -> None:
    global _running
    if _running or os.getenv("CARD_REFRESH", "1") == "0":
        return
    _running = True
    try:
        guild_ids = {g.id for g in client.guilds}
        members = {g.id: g.member_count or 0 for g in client.guilds}
        jobs = pending(guild_ids, members)
        if not jobs:
            return
        live_n = sum(1 for k, _ in jobs if k == "live")
        log.info("Card refresh starting: %d cards (%d live, %d claimed) in %d servers",
                 len(jobs), live_n, len(jobs) - live_n, len({r["guild_id"] for _, r in jobs}))
        tally: dict[str, int] = {}
        errors_in_a_row = 0
        for i, (kind, row) in enumerate(jobs, 1):
            result = await _redraw(client, kind, row)
            tally[result] = tally.get(result, 0) + 1
            if result != "error":
                _record(row["message_id"], kind, result)
                errors_in_a_row = 0
            else:
                errors_in_a_row += 1
                if errors_in_a_row >= 10:
                    log.warning("Card refresh paused after 10 errors in a row. It retries on next restart.")
                    break
            if i % 100 == 0:
                log.info("Card refresh progress %d/%d %s", i, len(jobs), tally)
            await asyncio.sleep(PACE_SECONDS)
        log.info("Card refresh done %s", tally)
    except Exception:
        log.exception("Card refresh stopped")
    finally:
        _running = False
