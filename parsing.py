"""
parsing.py — Input cleaning, link blocking, expiration parsing, bulk entry parsing.
"""
import re
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Link blocking
# ---------------------------------------------------------------------------

# Anything with a scheme or www is a link. A bare word.tld is a link only when
# the tld is a real, common one, so codes like ABCD.EFGH or names like
# Dr.Mario are no longer rejected by mistake.
_SCHEME_RE = re.compile(r"(?i)(https?://|www\.|discord\.gg/|discord(?:app)?\.com/invite)")
_BARE_TLDS = (
    "com|net|org|gg|io|co|xyz|ly|me|tv|app|dev|ru|uk|de|info|biz|link|shop|store|site|"
    "online|click|top|gift|gifts|cc|ws|to|us|ca|eu|fr|nl|pl|br|in|cn|jp|kr|au|tk|ml|ga|cf|gq|"
    "live|pro|fun|space|website|tech|club|vip|win|bid|stream|sbs|cfd|icu|cyou|rest|lol"
)
_BARE_RE = re.compile(rf"(?i)\b[a-z0-9-]+(\.[a-z0-9-]+)*\.({_BARE_TLDS})\b(/\S*)?")


def contains_link(text: str | None) -> bool:
    if not text:
        return False
    return bool(_SCHEME_RE.search(text) or _BARE_RE.search(text))


# ---------------------------------------------------------------------------
# Field cleaning
# ---------------------------------------------------------------------------

_JUNK_VALUES = frozenset({
    "unknown", "n/a", "na", "none", "no platform",
    "no expiration", "never", "no expiry", "no expire",
})


def clean_platform(platform: str | None) -> str:
    if not platform:
        return ""
    p = str(platform).strip()
    return "" if p.lower() in _JUNK_VALUES else p


def clean_expiration(expires_at: str | None) -> str:
    if not expires_at:
        return ""
    e = str(expires_at).strip()
    if e.lower() in _JUNK_VALUES:
        return ""
    return re.sub(r"^exp(?:ires)?\s+", "", e, flags=re.IGNORECASE).strip()


# ---------------------------------------------------------------------------
# Expiration
# ---------------------------------------------------------------------------

_US_DATE_RE  = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:[ T](\d{1,2}):(\d{2}))?")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?")
_RELATIVE_RE = re.compile(
    r"^(?:in\s+)?(\d{1,4})\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|wk|week|weeks)$",
    re.IGNORECASE,
)
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def normalize_expiration(raw: str | None) -> str:
    """
    Turn what a user typed into the stored form.
      "7d", "24h", "90m", "2 weeks", "in 3 days" -> "YYYY-MM-DD HH:MM" (UTC)
      "tomorrow" -> end of tomorrow UTC
      dates are stored as typed (12/31/2026 or 2026-12-31)
    Returns "" for no expiration. Validity is checked by expiration_is_valid().
    """
    e = clean_expiration(raw)
    if not e:
        return ""
    m = _RELATIVE_RE.match(e)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()[0]
        dt = datetime.now(timezone.utc) + timedelta(seconds=amount * _UNIT_SECONDS[unit])
        return dt.strftime("%Y-%m-%d %H:%M")
    if e.lower() == "tomorrow":
        dt = datetime.now(timezone.utc) + timedelta(days=1)
        return dt.strftime("%Y-%m-%d")
    if e.lower() == "today":
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return e


def parse_expiration_datetime(expires_at: str | None) -> datetime | None:
    e = clean_expiration(expires_at)
    if not e:
        return None
    for pattern, order in ((_US_DATE_RE, "mdy"), (_ISO_DATE_RE, "ymd")):
        m = pattern.search(e)
        if not m:
            continue
        try:
            if order == "mdy":
                month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if m.group(4) and m.group(5):
                return datetime(year, month, day, int(m.group(4)), int(m.group(5)), tzinfo=timezone.utc)
            # A bare date means "through the end of that day" (UTC).
            return datetime(year, month, day, tzinfo=timezone.utc) + timedelta(days=1)
        except ValueError:
            return None
    return None


def expiration_is_valid(expires_at: str | None) -> bool:
    e = clean_expiration(expires_at)
    return (not e) or (parse_expiration_datetime(e) is not None)


def is_expired(expires_at: str | None) -> bool:
    dt = parse_expiration_datetime(expires_at)
    return dt is not None and datetime.now(timezone.utc) >= dt


def expiry_timestamp(expires_at: str | None) -> int | None:
    dt = parse_expiration_datetime(expires_at)
    return int(dt.timestamp()) if dt else None


EXPIRY_HELP = "Use a date like 12/31/2026, or a length like 24h, 7d or 2w."


# ---------------------------------------------------------------------------
# Bulk parsing
# ---------------------------------------------------------------------------

def parse_bulk_label(label: str) -> tuple[str, str]:
    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", label.strip())
    if m:
        return m.group(1).strip(), clean_platform(m.group(2))
    return label.strip(), ""


def parse_bulk_entry(entry: str) -> tuple[str, str, str, str] | None:
    """Product Name (Platform): CODE | expiration  ->  (name, platform, code, expiration)."""
    entry = entry.strip()
    if ":" not in entry:
        return None
    raw_label, rest = entry.split(":", 1)
    raw_label, rest = raw_label.strip(), rest.strip()
    if not raw_label or not rest:
        return None
    if "|" in rest:
        product_code, expires_at = rest.rsplit("|", 1)
        product_code = product_code.strip()
        expires_at = normalize_expiration(expires_at)
    else:
        product_code, expires_at = rest.strip(), ""
    item_name, platform = parse_bulk_label(raw_label)
    if not item_name or not product_code:
        return None
    return item_name, platform, product_code, expires_at


def split_bulk_entries(raw_text: str) -> list[str]:
    if "\n" in raw_text:
        return [line.strip() for line in raw_text.splitlines() if line.strip()]
    if ";" in raw_text:
        return [e.strip() for e in raw_text.split(";") if e.strip()]
    return [raw_text.strip()] if raw_text.strip() else []


def parse_bulk_text(raw_text: str) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """Returns (valid entries, skipped lines with a reason)."""
    valid, skipped = [], []
    for item in split_bulk_entries(raw_text):
        parsed = parse_bulk_entry(item)
        if parsed is None:
            skipped.append(f"{item}  [needs Name: CODE]")
            continue
        item_name, platform, product_code, expires_at = parsed
        if expires_at and not expiration_is_valid(expires_at):
            skipped.append(f"{item}  [expiration not understood]")
            continue
        if is_expired(expires_at):
            skipped.append(f"{item}  [already expired]")
            continue
        valid.append((item_name, platform, product_code, expires_at))
    return valid, skipped
