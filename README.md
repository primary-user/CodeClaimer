# CodeClaimer

A Discord bot for small, trusted communities to share spare game keys, product codes, and access codes safely.

Instead of posting codes in public chat, members submit them through slash commands. CodeClaimer posts a public claim card, keeps the code hidden, sends it by DM to the first claimer, then marks the card as claimed.

---

## Features

- Hidden code sharing via slash commands
- `/sharecode` for single drops, `/bulkshare` for multiple
- Optional platform and expiration date fields
- Optional pre-drop message for bulk drops — post above the cards with mentions and context
- Expired unclaimed cards automatically marked as expired
- First-come, first-served DM delivery
- Claimed and expired cards are greyed out with the button removed
- Stale cards self-clean on next click
- **Claim Verification** — optional math challenge before claiming to slow fast claims and deter bots
- **One Claim Per Batch** — optionally limit members to one claim per `/bulkshare` drop
- Persistent SQLite database with WAL mode
- Persistent claim buttons across restarts and redeploys
- Per-server settings that survive bot restarts and DB wipes
- Anti-phishing link filter on all input fields
- `/settings` with Mods Only, One Claim Per Batch, and Claim Verification toggles
- Settings buttons are green when ON, red when OFF
- Mods Only is ON by default, all other settings are OFF by default

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

Single `/sharecode` drops are not subject to the One Claim Per Batch setting.

---

### `/bulkshare`

Opens a private panel. Click **Open Bulk Entry Form** to open the modal.

The modal has two fields:

**Optional message** — posts as a plain channel message above the claim cards. Supports Discord markdown and mentions like `@everyone` or role names. Useful for crediting a sharer or tagging a role.

**Codes** — one per line in this format:

```text
Product Name (Platform): Code | MM/DD/YYYY
Product Name: Code
```

Examples:

```text
Hollow Knight (Steam): ABC-123 | 12/31/2026
Celeste: DEF-456
Outer Wilds (Steam, courtesy of Username): GHI-789 | 10/01/2026
```

- `:` separates the product label from the code
- `|` precedes the optional expiration date
- Platform is flexible — supports any text including attribution
- Each valid line creates its own claim card
- All cards from one modal submission share a `batch_id` for One Claim Per Batch enforcement

---

### `/settings`

Opens the server settings panel. Only moderators can change settings.

Moderator = Administrator, Manage Server, or Manage Messages.

| Setting | Default | Description |
|---|---|---|
| Mods Only | ON | Restricts `/sharecode` and `/bulkshare` to moderators |
| One Claim Per Batch | OFF | Limits each member to one claim per `/bulkshare` drop |
| Claim Verification | OFF | Requires members to answer a math question before claiming |

All buttons are **green when ON** and **red when OFF**.

Settings are stored per server and persist across bot restarts, redeploys, and database wipes. `INSERT OR IGNORE` ensures existing settings are never overwritten.

---

### `/help`

Shows private usage instructions. Ephemeral.

---

## Claim Flow

1. A user shares a code via `/sharecode` or `/bulkshare`
2. An optional pre-drop message is posted above the cards (bulk only)
3. The code is stored privately in SQLite
4. A public claim card is posted with a **Claim Code 🎁** button
5. The first user to successfully claim receives the code by DM
6. The card is marked as claimed, the button is removed, and the DB row is deleted

**If Claim Verification is ON:**
- Clicking the button shows an ephemeral math question with 4 answer buttons
- The user has 30 seconds to answer
- Multiple users can have challenges open simultaneously — each gets their own random problem
- The first person to answer correctly and complete the flow gets the code
- If someone else claims during the 30s window, the late answerer gets a "already claimed" message
- Wrong answers or timeouts leave the card live for others

**If One Claim Per Batch is ON:**
- A user who has already claimed from a bulk drop cannot claim another card from the same batch
- The batch check runs before the math challenge is shown

If a code expires before being claimed, a background task (runs every 10 minutes) marks the card as expired. Clicking an expired card handles it immediately.

If a card has no matching DB row, the next click greys it out automatically.

---

## Expiration

Optional. Supported formats:

```text
12/31/2026
12/31/2026 23:59
```

Date-only values expire at the end of that day in UTC. `YYYY-MM-DD` is also accepted for backward compatibility.

---

## Claim Verification

When enabled, clicking **Claim Code 🎁** shows a private math challenge instead of immediately sending the code.

- Simple addition or subtraction — answer is always a single digit (1–9)
- 4 answer buttons with one correct answer and three decoys, randomly shuffled
- 30 second timeout — card stays live for others if unanswered
- Each user gets their own independent random problem
- The DB row is re-checked after a correct answer to handle simultaneous claims
- Designed to slow fast claiming and add friction against automated scripts

---

## One Claim Per Batch

When enabled, each server member can claim at most one code per `/bulkshare` drop. All cards posted from a single bulk submission share a `batch_id`. When a user claims any card in that batch, their user ID is recorded in the `batch_claims` table. Subsequent claim attempts from the same batch return an ephemeral rejection.

This setting does not apply to `/sharecode` single drops.

---

## Pre-Drop Message

The optional message field in the bulk share modal posts a plain channel message directly above the claim cards. Because it is posted as a normal message (not an embed), Discord resolves mentions correctly — `@everyone`, `@here`, and role names will ping if the bot has the Mention Everyone permission.

---

## Anti-Phishing

All input fields are checked for links and web addresses before posting. Submissions containing URLs are rejected with an ephemeral message to the submitter only.

---

## Database

SQLite with WAL journal mode. Three tables.

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
| `sharer_name` | Display name of the sharer at time of posting |
| `created_at` | UTC timestamp |
| `batch_id` | UUID linking cards from the same `/bulkshare` submission |

### `guild_settings`

| Column | Description |
|---|---|
| `guild_id` | Server ID |
| `mods_only` | `1` = ON, `0` = OFF |
| `one_claim_per_batch` | `1` = ON, `0` = OFF |
| `claim_verification` | `1` = ON, `0` = OFF |

### `batch_claims`

| Column | Description |
|---|---|
| `batch_id` | UUID of the bulk share batch |
| `user_id` | Discord user ID of the claimer |
| `guild_id` | Server ID |
| `claimed_at` | UTC timestamp |

Startup migrations handle new columns on existing databases without wiping data.

---

## Persistence

For claim buttons to survive restarts and redeploys:

1. Code data is saved in SQLite by `message_id`
2. Buttons have fixed `custom_id` values
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
DB_PATH=/app/data/codes.db
```

Mount a Railway volume at `/app/data`. The mount path and `DB_PATH` must match exactly. Without a mounted volume, the SQLite database is wiped on every redeploy.

Guild settings use `INSERT OR IGNORE` on every startup, so even if the database is wiped the bot re-seeds defaults for all current guilds without overwriting customized settings.

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

> **Read Message History** is required. The bot calls `fetch_message()` to edit claim cards after expiration or on restart. Without it, expired and stale cards will not update.

> **Mention Everyone** is required if mods want `@everyone` or `@here` in pre-drop messages to actually ping.

Privileged Gateway Intent:

```text
Message Content Intent
```

---

## Requirements

```text
discord.py
```

Standard library: `os`, `re`, `random`, `asyncio`, `sqlite3`, `uuid`, `datetime`

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
