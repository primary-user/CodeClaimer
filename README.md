# CodeClaimer

A Discord bot for small, trusted communities to share spare game keys, product codes, and access codes safely.

Instead of posting codes in public chat, members submit them through slash commands. CodeClaimer posts a public claim card, keeps the code hidden, sends it by DM to the first claimer, then marks the card as claimed.

---

## Features

- Hidden code sharing via slash commands
- `/sharecode` for single drops, `/bulkshare` for multiple
- Optional platform and expiration date fields
- Expired unclaimed cards automatically marked as expired
- First-come, first-served DM delivery
- Claimed and expired cards are greyed out with the button removed
- Stale cards (claimed elsewhere or after a restart) self-clean on next click
- Persistent SQLite database
- Persistent claim buttons across restarts and redeploys
- Anti-phishing link filter on all input fields
- `/settings` with Mods Only toggle
- Mods Only is ON by default

---

## Commands

### `/sharecode`

| Field | Required | Description |
|---|---|---|
| `item_name` | Yes | Game or product name |
| `code` | Yes | Private activation code |
| `platform` | No | Steam, Epic, PS5, Xbox, etc. |
| `expires_at` | No | Expiration date — `MM/DD/YYYY` |

```text
/sharecode item_name: Hollow Knight code: ABC-123 platform: Steam expires_at: 12/31/2026
/sharecode item_name: Celeste code: DEF-456
```

---

### `/bulkshare`

Opens a private panel. Click **Open Bulk Entry Form** and paste one code per line.

```text
Product Name (Platform): Code | MM/DD/YYYY
Product Name: Code
```

Examples:

```text
Hollow Knight (Steam): ABC-123 | 12/31/2026
Celeste: DEF-456
Minecraft Skin Pack (Xbox): GHI-789 | 10/01/2026
```

- `:` separates the product label from the code
- `|` precedes the optional expiration date
- Each valid line creates its own claim card

---

### `/settings`

Opens the server settings panel with a **Mods Only** toggle and Ko-fi support button.

Only moderators can change settings. Moderator = Administrator, Manage Server, or Manage Messages.

When Mods Only is ON, only moderators can use `/sharecode` and `/bulkshare`. Claiming is open to anyone who can see the card.

---

### `/help`

Shows private usage instructions. Ephemeral.

---

## Claim Flow

1. A user shares a code via `/sharecode` or `/bulkshare`
2. The code is stored privately in SQLite
3. A public claim card is posted with a **Claim Code 🎁** button
4. The first user to click receives the code by DM
5. The card is marked as claimed, the button is removed, and the DB row is deleted

If a code expires before being claimed, a background task (runs every 10 minutes) marks the card as expired and removes the button. If a user clicks an expired card before the task catches it, the bot handles it immediately.

If a card is left over from a previous session with no matching DB row (e.g. after a wipe), the next click on that card will grey it out and mark it as already claimed automatically.

---

## Expiration

Optional. Supported formats:

```text
12/31/2026
12/31/2026 23:59
```

Date-only values expire at the end of that day in UTC. The bot also accepts `YYYY-MM-DD` for backward compatibility.

---

## Anti-Phishing

All input fields are checked for links and web addresses before posting. Submissions containing URLs are rejected silently to the submitter only.

---

## Database

SQLite. Two tables.

### `shared_codes`

| Column | Description |
|---|---|
| `message_id` | Discord message ID of the claim card |
| `product_code` | Hidden code sent by DM |
| `item_name` | Product or game name |
| `platform` | Optional platform |
| `expires_at` | Optional expiration text |
| `guild_id` | Server ID |
| `channel_id` | Channel ID |
| `sharer_id` | User ID of the sharer |
| `created_at` | UTC timestamp |

### `guild_settings`

| Column | Description |
|---|---|
| `guild_id` | Server ID |
| `mods_only` | `1` = ON, `0` = OFF |

Startup migrations handle new columns on existing databases without wiping data.

---

## Persistence

For claim buttons to survive restarts and redeploys, three things must be true:

1. Code data is saved in SQLite by `message_id`
2. The button has a fixed `custom_id`
3. Both persistent views are re-registered on startup

```python
timeout=None
custom_id="codeclaimer_claim_code_btn"

# In setup_hook:
self.add_view(ClaimButtonView())
self.add_view(BulkSharePanelView())
```

The SQLite file must live on a mounted Railway volume for data to survive redeploys.

---

## Railway Setup

Required:

```env
DISCORD_TOKEN=your_discord_bot_token_here
```

Recommended:

```env
DB_PATH=/data/codes.db
```

Mount a Railway volume at `/data`. Without a mounted volume, the SQLite database may be wiped on redeploy.

---

## Discord Developer Portal

OAuth2 scopes:

```text
bot
applications.commands
```

Bot permissions:

```text
Send Messages
Embed Links
Manage Messages
Read Message History
```

> **Read Message History** is required. The bot calls `fetch_message()` to edit claim cards after expiration or on restart. Without it, expired and stale cards will not update and the bot will log a permissions error.

Privileged Gateway Intent:

```text
Message Content Intent
```

---

## Requirements

```text
discord.py
```

Standard library: `os`, `re`, `random`, `asyncio`, `sqlite3`, `datetime`

---

## Security Notes

- Never hardcode the bot token in `bot.py`
- Store it in Railway as `DISCORD_TOKEN`
- If a token was ever committed to GitHub, reset it immediately in the Discord Developer Portal
- Do not commit `.env` files, SQLite databases, or local data folders
- Mods Only is ON by default

---

## Future Ideas

- `/listcodes` — list active unclaimed codes
- `/cleanup` — remove stale cards
- `/repost` — repost a card after channel cleanup
- Claim history logs
- Role restrictions for claiming
- Per-user claim cooldowns
- Moderator exports

---

## License

MIT. Inspect, modify, self-host, and contribute freely.

---

## Support

[ko-fi.com/artchemylabs](https://ko-fi.com/artchemylabs)
