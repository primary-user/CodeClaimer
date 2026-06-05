# CodeClaimer

CodeClaimer is a custom Discord bot for small, high-trust communities that want a safe way to share spare product codes, game keys, and digital access codes.

Instead of posting codes publicly where anyone can copy them, members submit codes through slash commands. CodeClaimer posts a clean public claim card, DMs the hidden code to the first person who claims it, then updates the public post so the community can see the code has already been claimed.

---

## Current Features

- Secure code sharing through Discord slash commands
- Hidden product/game/access codes
- Public claim cards with product, platform, and sharer information
- First-come, first-served claiming
- Private DM delivery to the claimer
- Public post updates after a successful claim
- Claim button removal after claim
- Persistent SQLite database support
- Railway-ready environment variable setup
- Anti-phishing link filter
- `/sharecode` command for single code drops
- `/bulkshare` command for multiple code drops
- Bulk platform parsing using `Product Name (Platform) | Code`
- Randomized public card titles
- Ko-fi support link in successful claim DMs

---

## How It Works

1. A member submits a code using `/sharecode` or `/bulkshare`.
2. The bot checks the submission for links or suspicious web addresses.
3. The actual code is kept hidden from the public channel.
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

Shares multiple hidden codes at once.

#### Format

```text
Product Name (Platform) | Code
```

You can separate entries by new lines, commas, or semicolons.

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

If no platform is included in parentheses, the bot will use:

```text
Platform: Unknown
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

---

## Database

CodeClaimer uses SQLite for persistence.

The database stores active, unclaimed codes in the `shared_codes` table:

| Column | Description |
|---|---|
| `message_id` | Discord message ID for the public claim card |
| `product_code` | Hidden code to send by DM |
| `item_name` | Product/game/item name |
| `platform` | Platform associated with the code |

When a code is successfully claimed, the row is deleted from the database.

---

## Railway Environment Variables

CodeClaimer is set up to keep sensitive values out of GitHub.

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
- The anti-phishing filter helps reduce risk, but it is not a full moderation or malware detection system.

---

## Current Bot Behavior

### Successful Claim DM

When a user claims a code, they receive a clean DM embed containing:

- The product/item name
- The platform, when available
- The hidden product code
- A reminder to share extra keys using `/sharecode`
- A Ko-fi support link

### Public Claimed Message

After a successful claim, the public post changes to:

```text
Loot Claimed!

The code for Product Name has been successfully claimed by @claimer.

Thank you to @sharer for sharing with the community!
```

The claim button is removed.

---

## Project Status

Current CodeClaimer status:

- Bot token moved to Railway environment variable
- SQLite database added for persistence
- Railway deployment supported
- Anti-phishing filter added
- `/sharecode` command added
- `/bulkshare` command added
- Bulk platform parsing added
- Public cards now show product, platform, and sharer
- DM layout cleaned up
- Claimed posts are greyed out instead of deleted
- Ko-fi support link added to DMs

---

## Future Feature Ideas

Possible next features:

- Admin-only cleanup command
- Claim history log channel
- Optional cooldowns to prevent one user from claiming too many codes
- Role restrictions for sharing or claiming
- Better duplicate claim protection under heavy traffic
- Platform dropdown choices
- Anonymous sharing option
- Public stats command
- `/help` command
- Better bulk upload feedback showing skipped or malformed entries

---

## License

Private project unless otherwise specified.

---

## Maintainer

Built and maintained by the CodeClaimer project owner.
