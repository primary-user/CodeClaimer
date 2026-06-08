# CodeClaimer

CodeClaimer is a custom Discord bot for small, high-trust communities that want a safer way to share spare product codes, game keys, and digital access codes.

Instead of posting codes publicly where anyone can copy them, members submit codes through slash commands. CodeClaimer posts a public claim card, keeps the actual code hidden, DMs the code to the first person who claims it, then marks the public post as claimed.

---

## Current Features

- Secure code sharing through Discord slash commands
- Hidden product/game/access codes
- Public claim cards with product, platform, and sharer information
- First-come, first-served claiming
- Private DM delivery to the first claimer
- Public claim cards are greyed out after a successful claim
- Claim button is removed after a successful claim
- Persistent SQLite database support
- Railway-ready environment variable setup
- Anti-phishing link filter
- `/sharecode` command for single code drops
- `/bulkshare` guided panel for multiple code drops
- Bulk entry modal with a large multi-line text field
- Bulk parsing using `Product Name (Platform) | Code`
- Line-break bulk entry support
- Comma and semicolon fallback parsing
- `/help` command with ephemeral instructions
- `/settings` panel
- Mods Only access toggle
- Mods Only is ON by default for new servers
- Support CodeClaimer button linking to Ko-fi
- Randomized public card titles

---

## How It Works

1. A member submits a code using `/sharecode` or `/bulkshare`.
2. The bot checks the submission for links or suspicious web addresses.
3. The actual code stays hidden from the public channel.
4. The bot posts a public claim card showing:
   - Product
   - Platform
   - Shared by
5. The first member to click **Claim Code 🎁** receives the code by DM.
6. The public claim card is replaced with a grey claimed message.
7. The claim button is removed.
8. The claimed code is deleted from the SQLite database.

---

## Commands

### `/help`

Shows private instructions for using CodeClaimer.

The help panel explains:

- How to use `/sharecode`
- How to use `/bulkshare`
- The correct bulk format
- The rules for sharing codes
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

#### Mods Only Default

Mods Only is **ON by default** for new servers.

That means regular members cannot use `/sharecode` or `/bulkshare` until a moderator turns Mods Only off in `/settings`.

#### Mods Only: ON

Only moderators can use:

- `/sharecode`
- `/bulkshare`

#### Mods Only: OFF

Members with lower roles can use:

- `/sharecode`
- `/bulkshare`

Claiming a code is still available to members who can see and click the claim card.

---

### `/sharecode`

Shares one hidden code with the community.

#### Fields

| Field | Description |
|---|---|
| `item_name` | Name of the game, product, or item |
| `platform` | Platform the code is for, such as Steam, Epic, PS5, Xbox |
| `code` | The private activation/product/access code |

#### Example

```text
/sharecode item_name: Hollow Knight platform: Steam code: ABC-123-XYZ
```

#### Public Output

```text
Product: Hollow Knight
Platform: Steam
Shared by: @username

Click the button below to claim it instantly via DM.
```

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

#### Preferred Format

Use one code per line:

```text
Product Name (Platform) | Code
Product Name (Platform) | Code
Product Name (Platform) | Code
```

#### Example

```text
Hollow Knight (Steam) | ABC-123
Celeste (Epic) | DEF-456
Minecraft Skin Pack (Xbox) | GHI-789
```

#### Public Output For Each Entry

```text
Product: Hollow Knight
Platform: Steam
Shared by: @username

Click the button below to claim it instantly via DM.
```

#### Fallback Separators

Line breaks are recommended, but the parser also accepts:

- Commas
- Semicolons

Example fallback format:

```text
Hollow Knight (Steam) | ABC-123, Celeste (Epic) | DEF-456
```

---

## Bulk Share Panel

The `/bulkshare` command no longer forces the user to paste everything into a slash command field.

Instead, it opens a private instruction card with a button:

```text
Open Bulk Entry Form
```

The button opens a Discord modal with a large paragraph field. This allows users to paste multiple lines of text more naturally.

Preferred input:

```text
Hollow Knight (Steam) | ABC-123
Celeste (Epic) | DEF-456
Minecraft Skin Pack (Xbox) | GHI-789
```

---

## Anti-Phishing Protection

CodeClaimer rejects submissions containing links, websites, or web addresses.

This applies to:

- Product names
- Platform names
- Code fields
- Bulk submission text

Rejected submissions show this message:

```text
Submission Rejected: Links, websites, and web addresses are strictly prohibited to prevent phishing scams.
```

This is not a full moderation or malware detection system. It is a basic safety filter to reduce obvious phishing risk.

---

## Database

CodeClaimer uses SQLite for persistence.

The database stores active, unclaimed codes in the `shared_codes` table.

| Column | Description |
|---|---|
| `message_id` | Discord message ID for the public claim card |
| `product_code` | Hidden code to send by DM |
| `item_name` | Product/game/item name |
| `platform` | Platform associated with the code |

When a code is successfully claimed, the row is deleted from the database.

CodeClaimer also stores server settings in the `guild_settings` table.

| Column | Description |
|---|---|
| `guild_id` | Discord server ID |
| `mods_only` | `1` means Mods Only is ON, `0` means Mods Only is OFF |

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

## Railway Environment Variables

CodeClaimer keeps sensitive values out of GitHub.

### Required Variable

```env
DISCORD_TOKEN=your_discord_bot_token_here
```

This is your Discord bot token from the Discord Developer Portal.

### Recommended Variable For Persistent SQLite

```env
DB_PATH=/data/codes.db
```

Use this when you have a Railway volume mounted at `/data`.

If `DB_PATH` is not set, the bot defaults to:

```text
data/codes.db
```

For Railway production, use a mounted volume so the SQLite database survives redeploys.

---

## Railway Volume Requirement

If you want claim cards and settings to survive Railway redeploys, you need a Railway volume mounted at:

```text
/data
```

Then set:

```env
DB_PATH=/data/codes.db
```

Without a mounted volume, Railway may wipe the local SQLite file during redeploys.

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

---

## Recommended Railway Start Command

```bash
python bot.py
```

If Railway uses Python 3 explicitly:

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

Even though most of the bot uses slash commands, this was part of the current setup and should remain enabled unless the code is later adjusted.

---

## Security Notes

- Never hardcode the Discord bot token inside `bot.py`.
- Store the token in Railway as `DISCORD_TOKEN`.
- If the token was ever committed to GitHub, reset it in the Discord Developer Portal.
- Do not share screenshots or pasted code that include the token.
- Keep this bot intended for small, trusted communities.
- Mods Only is ON by default for safer server rollout.
- The anti-phishing filter helps reduce risk, but it is not a complete moderation system.

---

## Current Bot Behavior

### Successful Claim DM

When a user claims a code, they receive a clean DM embed containing:

- The product/item name
- The platform, when available
- The hidden product code
- A reminder to share extra keys using `/sharecode`

The DM no longer includes the Ko-fi support link.

### Public Claimed Message

After a successful claim, the public post changes to:

```text
Loot Claimed!

The code for Product Name has been successfully claimed by @claimer.

Thank you to @sharer for sharing with the community!
```

The claim button is removed.

---

## Support Button

The Ko-fi link now lives in the `/settings` panel as a button:

```text
Support CodeClaimer
```

It links to:

```text
https://ko-fi.com/artchemylabs
```

---

## Project Status

Current CodeClaimer status:

- Bot token moved to Railway environment variable
- SQLite database added for persistence
- Railway deployment supported
- Railway volume support documented
- Anti-phishing filter added
- `/sharecode` command added
- `/bulkshare` guided panel added
- Bulk modal form added
- Bulk line-break format added
- Bulk comma and semicolon fallback parsing added
- `/help` command added
- `/settings` command added
- Mods Only toggle added
- Mods Only is ON by default
- Public cards show product, platform, and sharer
- DM layout cleaned up
- Ko-fi removed from DM card
- Ko-fi support button added to settings panel
- Claimed posts are greyed out instead of deleted
- Persistent claim buttons survive restarts and redeploys when SQLite is persisted

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

---

## License

CodeClaimer is open-source under the MIT License.

You can inspect, modify, self-host, and contribute to the project. The official hosted bot is maintained independently and supported through Ko-fi donations.

---

## Support

If CodeClaimer is useful to your server, you can support development and hosting here:

[ko-fi.com/artchemylabs](https://ko-fi.com/artchemylabs)
