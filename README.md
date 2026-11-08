# CodeClaimer

**Private, fair code drops and raffles for Discord communities.**

Share spare game keys and codes without posting them in public. A code stays hidden until someone claims it. Then it shows on their screen, only to them, with a copy in their DMs.

[![discord.py](https://img.shields.io/badge/discord.py-2.6-5865F2?logo=discord&logoColor=white)](https://discordpy.readthedocs.io)
[![Top.gg](https://img.shields.io/badge/Top.gg-Add%20Bot-FF3366)](https://top.gg/bot/1511758084194832495)
[![Ko-fi](https://img.shields.io/badge/Ko--fi-Support-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/artchemylabs)
[![Support Server](https://img.shields.io/badge/Discord-Support-5865F2?logo=discord&logoColor=white)](https://discord.gg/NSt5Rcm8VN)

## How it works

**First claim drops.** Run `/sharecode` or `/bulkshare`. A card goes up with a Claim button. The first member to press it (and answer the claim question, if your server uses one) gets the code privately. The card flips to Claimed.

**Raffles.** Run `/raffle`, paste one or more codes and pick how long entries stay open. Members press Enter. When time runs out, winners are picked at random, one code each. They get pinged and press Show my prize to see their code.

## Features

- **Codes stay private.** Shown only to the winner, on screen, so closed DMs never block a claim. A DM copy is sent when DMs are open.
- **Show my code.** The winner can bring their code back from the card any time.
- **Raffles.** Up to 20 prizes, 1 minute to 14 days, random draw, restart safe.
- **Claim challenges.** Six modes to slow down bots, plus a 15 second lockout after a wrong answer.
- **One claim per bulk drop**, **role gate** and **per member cooldown**, all enforced in a single database transaction.
- **Withdraw code.** Right click a card, then Apps, then Withdraw code. Works on raffles too. The code goes back to the sharer. Deleted cards return the code as well.
- **Expiration.** A date (`12/31/2026`) or a length (`24h`, `7d`, `2w`). Cards show a live countdown and lock when time is up.
- **Animated claw tiles.** Each card shows the brand claw for its state: a light tile that teases the gift while live, a navy tile where the claw takes the gift and a check lands once claimed, and a grey empty claw when expired or withdrawn.
- **Link blocking** in every field.
- **Claim log** for mods, including raffle wins.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/sharecode` | Members or mods | Post one code as a claim card |
| `/bulkshare` | Members or mods | Post many codes, one card each |
| `/raffle` | Members or mods | Raffle one or more codes |
| `/settings` | Mods | Who can share and claim, claim challenge, cooldown |
| `/claimlog` | Mods | Who claimed what |
| `/help` | Everyone | How it works |
| Withdraw code | Sharer or mods | Message command on a card |

Mod means Administrator, Manage Server or Manage Messages. Sharing defaults to mods only.

## Bulk and raffle format

One code per line. Platform and expiration are optional.

```
Hollow Knight (Steam): ABC-123 | 12/31/2026
Celeste: DEF-456 | 7d
Minecraft Skin Pack (Xbox): GHI-789
```

## Settings

| Setting | Default | What it does |
|---|---|---|
| Who can share | Mods only | Who can post drops and raffles |
| One claim per bulk drop | On | One code per member per `/bulkshare` drop |
| Claim challenge | On, Easy | Question before claiming or entering a raffle |
| Who can claim | Everyone | Limit claiming and raffle entry to roles |
| Claim cooldown | Off | Minutes between claims per member |

Challenge modes: Easy, Medium, Hard, Difficulty Over Time, Periodic Table, Random.

## Project layout

| File | Purpose |
|---|---|
| `bot.py` | Startup, component router, background loops, events |
| `db.py` | SQLite schema, add only migrations, boot backup, the atomic claim |
| `cards.py` | Components V2 cards, private reveal, DM receipt |
| `claims.py` | Posting drops, claim flow, withdraw, deleted cards |
| `raffle.py` | Raffle form, entries, draw loop, cancel |
| `commands.py` | Slash commands and panels |
| `challenges.py` | Claim challenge generators |
| `parsing.py` | Input cleaning, link blocking, expirations, bulk format |
| `assets/` | Claw tiles, served to Discord through jsDelivr |
| `tools/build_claw.py` | Rebuilds the claw tiles from `assets/codeclaimer_icon.jpg` |

## Running it

Railway, one worker, a volume mounted at `/app/data`.

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Bot token |
| `DB_PATH` | `/app/data/codes.db` on Railway |
| `TOPGG_TOKEN` | Optional. Posts the server count to Top.gg |
| `CLAW_TILES` | Optional. `0` hides the claw tiles |

On every boot the database is copied to `data/backups/` (newest 2 kept) before migrations run. Migrations only add tables and columns, so cards posted by any earlier version keep working.

Logs show one line per drop, claim, raffle entry, draw, join and leave, plus a daily stats line.

## License

MIT. Built by [Artchemy Labs](https://artchemylabs.com).
