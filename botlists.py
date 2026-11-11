"""
Server counts for bot lists. Each list is posted only when its token is set
in the environment, so adding a list is one Railway variable.

  TOPGG_TOKEN     top.gg
  DBL_TOKEN       discordbotlist.com
  DBOTS_TOKEN     discord.bots.gg
  DISCORDS_TOKEN  discords.com (DISCORDS_URL overrides the endpoint)

Logs only when a count changes or a post fails, so the log stays quiet.
"""
import logging
import os

import aiohttp

log = logging.getLogger("codeclaimer.botlists")


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def targets(bot_id: int) -> list[dict]:
    """Lists with a token set. url, json body builder and token for each."""
    out = []
    spec = [
        ("Top.gg", "TOPGG_TOKEN", f"https://top.gg/api/bots/{bot_id}/stats",
         lambda n: {"server_count": n}),
        ("discordbotlist.com", "DBL_TOKEN", f"https://discordbotlist.com/api/v1/bots/{bot_id}/stats",
         lambda n: {"guilds": n}),
        ("discord.bots.gg", "DBOTS_TOKEN", f"https://discord.bots.gg/api/v1/bots/{bot_id}/stats",
         lambda n: {"guildCount": n}),
        ("discords.com", "DISCORDS_TOKEN",
         _env("DISCORDS_URL") or f"https://discords.com/bots/api/bot/{bot_id}",
         lambda n: {"server_count": n}),
    ]
    for name, env, url, body in spec:
        token = _env(env)
        if token:
            out.append({"name": name, "url": url, "body": body, "token": token})
    return out


_last: dict[str, int] = {}


async def _post(session: aiohttp.ClientSession, t: dict, count: int) -> tuple[int, str]:
    """Raw token first. Some lists want a "Bot " prefix, so retry with it on 401."""
    for auth in (t["token"], f"Bot {t['token']}"):
        async with session.post(t["url"], json=t["body"](count), headers={"Authorization": auth},
                                timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 401 and not auth.startswith("Bot ") and not t["token"].startswith("Bot "):
                continue
            return resp.status, (await resp.text())[:200]
    return 401, "unauthorized"


async def post_all(session: aiohttp.ClientSession | None, bot_id: int, count: int) -> None:
    if session is None:
        return
    for t in targets(bot_id):
        try:
            status, text = await _post(session, t, count)
            if status >= 400:
                log.warning("%s stats HTTP %d: %s", t["name"], status, text)
                continue
            if _last.get(t["name"]) != count:
                log.info("%s server count posted: %d", t["name"], count)
                _last[t["name"]] = count
        except Exception as e:
            log.warning("%s stats failed: %s", t["name"], e)
