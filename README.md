# CodeClaimer

**Private, fair, and verified code drops for Discord communities.**

CodeClaimer lets your community share spare game keys, access codes, and digital products securely. Codes stay hidden from the channel — only the first person to successfully claim one receives it, delivered privately by DM.

[![discord.py](https://img.shields.io/badge/discord.py-2.4.0-5865F2?logo=discord&logoColor=white)](https://discordpy.readthedocs.io)
[![Top.gg](https://img.shields.io/badge/Top.gg-Add%20Bot-FF3366)](https://top.gg/bot/1511758084194832495)
[![Ko-fi](https://img.shields.io/badge/Ko--fi-Support-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/artchemylabs)
[![Support Server](https://img.shields.io/badge/Discord-Support-5865F2?logo=discord&logoColor=white)](https://discord.gg/NSt5Rcm8VN)

---

## How It Works

A member or moderator runs `/sharecode` or `/bulkshare`. A claim card appears in the channel with a **Claim Code** button. The first person to press it — and pass the verification challenge, if enabled — gets the code delivered by DM. The card goes dark for everyone else.

---

## Features

- **Codes stay private** — never shown publicly; delivered to the winner by DM only
- **Race-condition safe** — atomic `DELETE … RETURNING` guarantees exactly one claimer per code, no matter how many people click simultaneously
- **Claim verification** — six built-in challenge modes to stop bots and autoclickers
- **Bulk drops** — share dozens of codes at once via a guided modal panel
- **Role gating** — restrict claiming to one or more specific server roles
- **Per-user cooldown** — enforce a minimum wait between claims per member
- **One claim per batch** — prevent any member from claiming multiple codes in a single bulk drop
- **Expiration** — codes auto-expire on a set date; cards grey out automatically
- **Mod audit log** — paginated, filterable claim history for moderators
- **Anti-phishing** — links are blocked across all input fields

---

## Commands

| Command | Access | Description |
|---|---|---|
| `/sharecode` | Members or mods | Share a single code as a claim card |
| `/bulkshare` | Members or mods | Open a panel to share multiple codes at once |
| `/settings` | Moderators only | Configure all per-server settings |
| `/claimlog` | Moderators only | View paginated claim history |
| `/help` | Everyone | Usage guide |

Moderator = Administrator, Manage Server, or Manage Messages.

---

## Settings

All settings are configured per-server via `/settings` and survive restarts and redeployments.

| Setting | Default | Description |
|---|---|---|
| Mods Only | On | Restrict `/sharecode` and `/bulkshare` to moderators |
| One Claim Per Batch | On | One code per member per bulk drop |
| Claim Verification | On | Require a challenge answer before delivering a code |
| Verification Mode | Easy | Challenge type (see Verification Modes) |
| Role Gate | Off | Limit claiming to members with at least one selected role |
| Claim Cooldown | Off | Minimum minutes between claims per user |

---

## Verification Modes

| Mode | Description |
|---|---|
| Easy | Simple addition or subtraction |
| Medium | PEMDAS expression, answer under 100 |
| Hard | Algebraic equation — solve for x |
| Difficulty Over Time | Starts Hard, difficulty drops 75% per tier on a timer down to an Easy floor that holds indefinitely |
| Periodic Table | Identify the symbol for a named element |
| Random | A fresh mode is selected for every individual claim |

---

## Claim Log

`/claimlog` is mod-only and shows the last 30 claims for your server in a paginated embed (10 per page).

- No filter — last 30 claims, newest first
- `user:` filter — all claims by a specific member, including their Discord ID for moderation actions
- `item:` filter — partial name match, e.g. `omni` returns all Omni drops
- Both filters can be combined

---

## Self-Hosting

The bot is publicly hosted and free to add — self-hosting is optional.

**Requirements**

- Python 3.11+
- `discord.py==2.4.0`
- `aiohttp==3.11.11`

**Environment variables**

```
DISCORD_TOKEN=your_bot_token
TOPGG_TOKEN=your_topgg_token    # optional — enables server count posting
```

**Run**

```bash
pip install -r requirements.txt
python bot.py
```

The database (`codeclaimer.db`) is created automatically on first run using SQLite with WAL mode. If hosting on Railway, mount a persistent volume and point the DB path to it.

---

## Links

- [Add to your server](https://top.gg/bot/1511758084194832495)
- [Vote on Top.gg](https://top.gg/bot/1511758084194832495/vote)
- [Support server](https://discord.gg/NSt5Rcm8VN)
- [Support development on Ko-fi](https://ko-fi.com/artchemylabs)
- [artchemylabs.com](https://artchemylabs.com)

---

*Built by [Artchemy Labs](https://artchemylabs.com)*
