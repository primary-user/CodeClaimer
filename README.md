# CodeClaimer

CodeClaimer is a custom Discord bot for small, trusted Discord communities that want a safer way to share spare product codes, game keys, and digital access codes.

Instead of posting codes directly in public chat, members submit them through slash commands. CodeClaimer posts a clean public claim card, keeps the actual code hidden, DMs the code to the first person who claims it, then marks the public post as claimed.

---

## Features

- Secure hidden code sharing through slash commands
- Public claim cards with product, optional platform, and sharer information
- First-come, first-served claiming
- Private DM delivery to the first claimer
- Claimed cards are greyed out and the claim button is removed
- Persistent SQLite database support
- Persistent claim buttons across bot restarts and redeploys
- Server metadata stored for future admin/stat commands
- Railway-ready environment variable setup
- Anti-phishing link filter
- `/sharecode` for single code drops
- `/bulkshare` guided panel with a multi-line modal form
- `/help` with private instructions
- `/settings` panel with Mods Only toggle and Ko-fi support button
- Mods Only is ON by default for new servers
- Optional platform field
- Randomized public card titles

---

## How It Works

1. A member submits a code using `/sharecode` or `/bulkshare`.
2. CodeClaimer checks the submission for links or suspicious web addresses.
3. The actual code stays hidden from the public channel.
4. The bot posts a public claim card showing the product, optional platform, and sharer.
5. The first member to click **Claim Code 🎁** receives the code by DM.
6. The public claim card is marked as claimed.
7. The claim button is removed.
8. The claimed code is deleted from SQLite.

---

## Commands

### `/help`

Shows private instructions for using CodeClaimer.

The help panel explains:

- How to use `/sharecode`
- How to use `/bulkshare`
- That platform is optional
- The sharing rules
- Where to find `/settings`

The help response is ephemeral, so only the user who runs the command sees it.

---

### `/settings`

Opens the CodeClaimer settings panel.

The settings panel includes:

- **Mods Only: ON/OFF** toggle
- **Support CodeClaimer** button

Only moderators can change settings.

Moderator access is based on one of these Discord permissions:

- Administrator
- Manage Server
- Manage Messages

Mods Only is **ON by default** for new servers.

When Mods Only is ON, only moderators can use:

- `/sharecode`
- `/bulkshare`

When Mods Only is OFF, members with lower roles can use those commands too.

Claiming a visible code is still available to members who can see and click the claim card.

---

### `/sharecode`

Shares one hidden code with the community.

#### Fields

| Field | Description |
|---|---|
| `item_name` | Name of the game, product, or item |
| `code` | Private activation/product/access code |
| `platform` | Optional platform, such as Steam, Epic, PS5, Xbox |

#### Example With Platform

```text
/sharecode item_name: Hollow Knight code: ABC-123-XYZ platform: Steam
```

#### Example Without Platform

```text
/sharecode item_name: Celeste code: DEF-456-XYZ
```

#### Public Output With Platform

```text
Product: Hollow Knight
Platform: Steam
Shared by: @username

Click the button below to claim it instantly via DM.
```

#### Public Output Without Platform

```text
Product: Celeste
Shared by: @username

Click the button below to claim it instantly via DM.
```

When platform is left blank, the platform line is omitted. The bot does not show `Unknown`.

---

### `/bulkshare`

Opens a private guided bulk-sharing panel.

The user flow is:

1. Run `/bulkshare`
2. Read the private instruction panel
3. Click **Open Bulk Entry Form**
4. Paste multiple code entries into the modal
5. Submit the form
6. CodeClaimer posts one claim card per valid entry

Use one code per line.

#### Format

```text
Product Name (Platform) | Code
Product Name | Code
```

Platform is optional.

#### Example

```text
Hollow Knight (Steam) | ABC-123
Celeste | DEF-456
Minecraft Skin Pack (Xbox) | GHI-789
```

Each valid line creates its own claim card.

---

## Optional Platform Behavior

Platform is optional in both `/sharecode` and `/bulkshare`.

The bot treats these platform values as blank:

```text
Unknown
N/A
NA
None
No Platform
```

If platform is blank or treated as blank, CodeClaimer omits the platform line from:

- The public claim card
- The DM claim card
- The public claimed message

This keeps cards cleaner and avoids unnecessary placeholder text.

---

## Anti-Phishing Protection

CodeClaimer rejects submissions containing links, websites, or web addresses.

This applies to:

- Product names
- Platform names
- Code fields
- Bulk submission text

Rejected submissions show:

```text
Submission Rejected: Links, websites, and web addresses are strictly prohibited to prevent phishing scams.
```

This is a basic safety filter to reduce obvious phishing risk. It is not a full moderation or malware detection system.

---

## Database

CodeClaimer uses SQLite for persistence.

### `shared_codes`

Stores active, unclaimed codes.

| Column | Description |
|---|---|
| `message_id` | Discord message ID for the public claim card |
| `product_code` | Hidden code to send by DM |
| `item_name` | Product/game/item name |
| `platform` | Optional platform associated with the code |
| `guild_id` | Discord server ID where the claim card was posted |
| `channel_id` | Discord channel ID where the claim card was posted |
| `sharer_id` | Discord user ID of the member who shared the code |
| `created_at` | UTC timestamp for when the claim card was created |

When a code is successfully claimed, the row is deleted from the database.

### `guild_settings`

Stores server-specific settings.

| Column | Description |
|---|---|
| `guild_id` | Discord server ID |
| `mods_only` | `1` means Mods Only is ON, `0` means Mods Only is OFF |

---

## Server Data Separation

CodeClaimer stores `guild_id`, `channel_id`, and `sharer_id` with every shared code.

The claim system still uses `message_id` as the primary lookup because Discord message IDs are globally unique. The added metadata prepares the bot for future server-specific tools like:

- `/listcodes`
- `/cleanup`
- `/serverstats`
- `/repost`
- Claim history logs
- Moderator exports

---

## Database Migration

CodeClaimer includes automatic migration support for the `shared_codes` table.

If an existing SQLite database was created before the metadata columns existed, the bot adds them automatically on startup:

```text
guild_id
channel_id
sharer_id
created_at
```

This lets existing installs update without wiping active data.

---

## Persistence

CodeClaimer is designed so active claim cards and settings survive bot restarts, Railway redeploys, and new GitHub commits.

Persistence depends on three things:

1. Active code data is saved in SQLite by Discord `message_id`.
2. The claim button uses a fixed persistent `custom_id`.
3. The bot re-registers the claim button view on startup using `setup_hook()`.

The persistent claim button uses:

```python
timeout=None
```

and a fixed button ID:

```python
custom_id="codeclaimer_claim_code_btn"
```

The bot also runs:

```python
self.add_view(ClaimButtonView())
```

inside `setup_hook()`.

This is what lets older claim buttons continue working after a restart or redeploy.

---

## Railway Setup

CodeClaimer keeps sensitive values out of GitHub.

### Required Variable

```env
DISCORD_TOKEN=your_discord_bot_token_here
```

### Recommended Variable For Persistent SQLite

```env
DB_PATH=/data/codes.db
```

Use this when you have a Railway volume mounted at:

```text
/data
```

If `DB_PATH` is not set, the bot defaults to:

```text
data/codes.db
```

For Railway production, use a mounted volume so the SQLite database survives redeploys. Without a mounted volume, Railway may wipe the local SQLite file during redeploys.

---

## Required Python Packages

Create a `requirements.txt` file with:

```text
discord.py
```

The bot also uses Python standard library modules:

- `os`
- `re`
- `random`
- `asyncio`
- `sqlite3`
- `datetime`

---

## Recommended Start Command

```bash
python bot.py
```

If your environment uses Python 3 explicitly:

```bash
python3 bot.py
```

---

## Discord Developer Portal Setup

### OAuth2 Scopes

Use these scopes:

```text
bot
applications.commands
```

### Bot Permissions

Use these permissions:

```text
Send Messages
Manage Messages
Read Message History
```

### Privileged Gateway Intents

Enable:

```text
Message Content Intent
```

Most of the bot uses slash commands, but this was part of the current setup and should remain enabled unless the code is later adjusted.

---

## Security Notes

- Never hardcode the Discord bot token inside `bot.py`.
- Store the token in Railway as `DISCORD_TOKEN`.
- If the token was ever committed to GitHub, reset it in the Discord Developer Portal.
- Do not share screenshots or pasted code that include the token.
- Mods Only is ON by default for safer server rollout.
- The anti-phishing filter helps reduce risk, but it is not a complete moderation system.

---

## Support

The Ko-fi link lives in the `/settings` panel as a button:

```text
Support CodeClaimer
```

It links to:

```text
https://ko-fi.com/artchemylabs
```

You can also support development and hosting here:

[ko-fi.com/artchemylabs](https://ko-fi.com/artchemylabs)

---

## Future Feature Ideas

Possible next features:

- Admin cleanup command
- Claim history log channel
- Optional cooldowns to prevent one user from claiming too many codes
- Role restrictions for claiming, not just sharing
- Platform dropdown choices
- Anonymous sharing option
- Public stats command
- Better duplicate claim protection under heavy traffic
- Better skipped-entry reporting for bulk uploads
- Exportable claim/share history for moderators
- Server-specific active code list
- Repost active code cards after channel cleanup

---

## License

CodeClaimer is open-source under the MIT License.

You can inspect, modify, self-host, and contribute to the project. The official hosted bot is maintained independently and supported through Ko-fi donations.
