# CodeClaimer

CodeClaimer is a Discord bot for small, trusted communities that want a safer way to share spare game keys, product codes, access codes, and digital rewards.

Instead of posting codes directly in public chat, members submit them through slash commands. CodeClaimer posts a public claim card, keeps the actual code hidden, sends the code by DM to the first person who claims it, then marks the public post as claimed.

---

## Features

- Hidden code sharing through Discord slash commands
- `/sharecode` for single code drops
- `/bulkshare` guided panel with a multi-line modal form
- Optional platform field
- Optional expiration date field
- Expired unclaimed cards are automatically marked as expired
- First-come, first-served claiming
- Private DM delivery to the first claimer
- Claimed cards are greyed out and the claim button is removed
- Persistent SQLite database support
- Persistent claim buttons across restarts and redeploys
- Server metadata stored for future admin/stat commands
- Anti-phishing link filter
- `/help` with private instructions
- `/settings` with Mods Only toggle and Ko-fi support button
- Mods Only is ON by default for new servers
- Railway-ready environment variable setup

---

## Commands

### `/sharecode`

Shares one hidden code.

| Field | Required | Description |
|---|---:|---|
| `item_name` | Yes | Game, product, or item name |
| `code` | Yes | Private activation/product/access code |
| `platform` | No | Platform such as Steam, Epic, PS5, Xbox |
| `expires_at` | No | Expiration date in `MM/DD/YYYY` format |

Example with platform and expiration:

```text
/sharecode item_name: Hollow Knight code: ABC-123 platform: Steam expires_at: 12/31/2026
```

Example without platform or expiration:

```text
/sharecode item_name: Celeste code: DEF-456
```

If platform is blank, the platform line is omitted from the public card and DM. The bot does not show `Unknown`.

---

### `/bulkshare`

Opens a private guided panel. The user clicks **Open Bulk Entry Form**, then pastes multiple entries into a Discord modal.

Use one code per line.

Format:

```text
Product Name (Platform): Code | Optional Expiration
Product Name: Code
```

Examples:

```text
Hollow Knight (Steam): ABC-123 | 12/31/2026
Celeste: DEF-456
Minecraft Skin Pack (Xbox): GHI-789 | 10/01/2026
```

Notes:

- `:` separates the product label from the code.
- `|` is only used before the optional expiration date.
- Platform is optional.
- Expiration is optional.
- Expiration should use `MM/DD/YYYY`.
- Each valid line creates its own claim card.

---

### `/help`

Shows private usage instructions for:

- `/sharecode`
- `/bulkshare`
- Optional platform
- Optional expiration
- `/settings`
- Sharing rules

The response is ephemeral.

---

### `/settings`

Opens the server settings panel.

Includes:

- **Mods Only: ON/OFF** toggle
- **Support CodeClaimer** button

Only moderators can change settings.

Moderator access is based on any of these Discord permissions:

- Administrator
- Manage Server
- Manage Messages

Mods Only is **ON by default** for new servers.

When Mods Only is ON, only moderators can use `/sharecode` and `/bulkshare`. Claiming visible codes remains available to members who can see and click the claim card.

---

## Claim Flow

1. A user shares a code with `/sharecode` or `/bulkshare`.
2. CodeClaimer checks the submission for links or web addresses.
3. The actual code is stored privately in SQLite.
4. A public claim card is posted.
5. The first user to click **Claim Code 🎁** receives the code by DM.
6. The public card is marked as claimed.
7. The button is removed.
8. The code row is deleted from SQLite.

If a code expires before being claimed, the card updates to show that it was unclaimed and expired, the button is removed, and the row is deleted from SQLite.

---

## Public Claim Card

With platform and expiration:

```text
Product: Hollow Knight
Platform: Steam
Expires: 12/31/2026
Shared by: @username

Click the button below to claim it instantly via DM.
```

Without platform or expiration:

```text
Product: Celeste
Shared by: @username

Click the button below to claim it instantly via DM.
```

---

## Expiration Behavior

Expiration is optional.

Supported formats:

```text
12/31/2026
12/31/2026 23:59
```

The bot also accepts `YYYY-MM-DD` internally for backward compatibility, but user-facing instructions use `MM/DD/YYYY`.

Date-only expirations are treated as valid through the listed date and expire after that day in UTC.

A background task checks for expired unclaimed codes every 10 minutes. If a user clicks an expired card before the checker catches it, the bot marks it expired immediately.

Expired card text:

```text
Code Expired

The code for Product Name was not claimed before it expired.

Expired: MM/DD/YYYY
```

---

## Anti-Phishing Protection

CodeClaimer rejects submissions containing links, websites, or web addresses.

Checked fields:

- Product names
- Platform names
- Code fields
- Expiration text
- Bulk submission text

This is a basic safety filter. It is not a full moderation or malware detection system.

---

## Database

CodeClaimer uses SQLite.

### `shared_codes`

Stores active, unclaimed codes.

| Column | Description |
|---|---|
| `message_id` | Discord message ID for the public claim card |
| `product_code` | Hidden code sent by DM |
| `item_name` | Product/game/item name |
| `platform` | Optional platform |
| `expires_at` | Optional expiration text |
| `guild_id` | Discord server ID |
| `channel_id` | Discord channel ID |
| `sharer_id` | Discord user ID of the member who shared the code |
| `created_at` | UTC timestamp for when the claim card was created |

### `guild_settings`

Stores server-specific settings.

| Column | Description |
|---|---|
| `guild_id` | Discord server ID |
| `mods_only` | `1` means Mods Only is ON, `0` means Mods Only is OFF |

The bot includes startup migrations for new `shared_codes` columns, so existing databases can update without wiping active data.

---

## Persistence

CodeClaimer is designed so active claim buttons work after bot restarts, Railway redeploys, and GitHub commits.

Persistence depends on:

1. Active code data saved in SQLite by Discord `message_id`
2. A fixed persistent button `custom_id`
3. Re-registering the persistent view during startup

Relevant code behavior:

```python
timeout=None
custom_id="codeclaimer_claim_code_btn"
self.add_view(ClaimButtonView())
```

For persistence to survive Railway redeploys, the SQLite file must live on a mounted Railway volume.

---

## Railway Setup

Required variable:

```env
DISCORD_TOKEN=your_discord_bot_token_here
```

Recommended for persistent SQLite:

```env
DB_PATH=/data/codes.db
```

Mount a Railway volume at:

```text
/data
```

If `DB_PATH` is not set, the bot defaults to:

```text
data/codes.db
```

Without a mounted volume, Railway may wipe the local SQLite database during redeploys.

---

## Requirements

`requirements.txt`:

```text
discord.py
```

Standard library modules used:

- `os`
- `re`
- `random`
- `asyncio`
- `sqlite3`
- `datetime`

---

## Start Command

```bash
python bot.py
```

or:

```bash
python3 bot.py
```

---

## Discord Developer Portal Setup

OAuth2 scopes:

```text
bot
applications.commands
```

Recommended bot permissions:

```text
Send Messages
Manage Messages
Read Message History
```

Privileged Gateway Intent currently used:

```text
Message Content Intent
```

Most of the bot uses slash commands, but this was part of the current setup and should remain enabled unless the code is later adjusted.

---

## Security Notes

- Never hardcode the Discord bot token in `bot.py`.
- Store the token in Railway as `DISCORD_TOKEN`.
- If a token was ever committed to GitHub, reset it in the Discord Developer Portal.
- Do not commit `.env`, SQLite databases, or local data folders.
- Mods Only is ON by default for safer rollout.
- The anti-phishing filter helps reduce obvious risk but does not replace moderation.

---

## Future Feature Ideas

The database now stores enough metadata to support:

- `/listcodes`
- `/cleanup`
- `/serverstats`
- `/repost`
- Claim history logs
- Moderator exports
- Server-specific active code lists
- Reposting active code cards after channel cleanup
- Role restrictions for claiming
- Cooldowns to prevent one user from claiming too many codes

---

## License

CodeClaimer is open-source under the MIT License.

You can inspect, modify, self-host, and contribute to the project. The official hosted bot is maintained independently and supported through Ko-fi donations.

---

## Support

Support development and hosting:

[ko-fi.com/artchemylabs](https://ko-fi.com/artchemylabs)
