"""Read-only ICS subscription feeds served alongside the CalDAV calendars.

Apple stores "subscribed calendars" (team schedules, holiday feeds, ...) as
device-side subscriptions: they are never exposed over CalDAV, so they cannot be
discovered or read through the CalDAV protocol at all. The underlying data is
just an iCalendar document at an HTTP(S) URL, so this module fetches those URLs
directly and parses their VEVENTs, giving the server a second, parallel source
that the existing read tools can serve.

Subscriptions are always read-only: nothing here writes back to a feed.

The pull list lives on a mounted volume (SUBSCRIPTIONS_FILE, default
/data/subscriptions.json) so runtime additions survive container restarts, and is
seeded from the optional SUBSCRIBED_ICS env var on startup.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
import recurring_ical_events
from filelock import FileLock
from icalendar import Calendar as ICalendar

logger = logging.getLogger("caldav-mcp.subscriptions")

# --- Configuration (all from env) ---------------------------------------------
# Where the pull list is persisted. Must be on a mounted volume for runtime
# additions to survive a container restart.
SUBSCRIPTIONS_FILE = os.environ.get("SUBSCRIPTIONS_FILE", "/data/subscriptions.json")
# Optional seed: a JSON object mapping display name -> feed URL, merged into the
# persisted list on startup. Handy for declaring feeds in compose.
SUBSCRIBED_ICS = os.environ.get("SUBSCRIBED_ICS", "")
# Per-request network timeout (seconds) for feed fetches. Same reasoning as
# CALDAV_TIMEOUT: keep it below the fronting proxy's gateway timeout so a slow
# feed surfaces as a clean tool error rather than a 502.
ICS_TIMEOUT = int(os.environ.get("ICS_TIMEOUT", "20"))
# How long a fetched feed is reused before revalidating, in seconds. Feeds change
# rarely and can be large, so this keeps repeated queries cheap.
ICS_CACHE_TTL = int(os.environ.get("ICS_CACHE_TTL", "900"))
# Comma-separated allowlist of subscription names/ids. When set, only these may be
# read. Separate from ALLOWED_CALENDARS: subscriptions are never writable, so the
# two lists govern different risks.
ALLOWED_SUBSCRIPTIONS = [
    s.strip() for s in os.environ.get("ALLOWED_SUBSCRIPTIONS", "").split(",") if s.strip()
]
# Feed URLs are fetched by the server, so a caller could otherwise use
# add_subscription to probe the internal network (SSRF). Private/loopback/
# link-local targets are refused unless this is explicitly enabled — only turn it
# on to subscribe to a feed hosted on your own LAN.
ICS_ALLOW_PRIVATE_IPS = os.environ.get("ICS_ALLOW_PRIVATE_IPS", "false").lower() == "true"

_MAX_REDIRECTS = 5

# Feed body cache keyed by normalized URL:
#   {"text": str, "etag": str|None, "last_modified": str|None, "fetched_at": float}
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


# --- URL handling --------------------------------------------------------------


def normalize_url(url: str) -> str:
    """Normalize a feed URL, rejecting anything that isn't an ICS-over-HTTP feed.

    Apple hands out `webcal://` links; those are plain HTTPS under a different
    scheme name, so they are rewritten rather than refused.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise ValueError("Feed URL is required.")
    lowered = candidate.lower()
    if lowered.startswith("webcal://"):
        candidate = "https://" + candidate[len("webcal://") :]
    elif lowered.startswith("webcals://"):
        candidate = "https://" + candidate[len("webcals://") :]
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Unsupported URL scheme {parsed.scheme!r}: feeds must be http(s) or webcal."
        )
    if not parsed.hostname:
        raise ValueError(f"Feed URL {url!r} has no host.")
    return candidate


def _assert_public_host(url: str) -> None:
    """Refuse URLs that resolve to private/loopback/link-local addresses.

    The server fetches these URLs on the caller's behalf, so without this an
    operator-facing tool doubles as an internal port scanner. Checked for every
    redirect hop, not just the original URL.
    """
    if ICS_ALLOW_PRIVATE_IPS:
        return
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve feed host {host!r}: {exc}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            raise ValueError(
                f"Feed host {host!r} resolves to non-public address {addr}. "
                "Set ICS_ALLOW_PRIVATE_IPS=true to allow LAN-hosted feeds."
            )


def make_id(name: str, url: str) -> str:
    """Stable id: a slug of the name plus a short hash of the URL.

    The URL is the real identity (names collide and can be renamed), so the hash
    is what makes this unique; the slug just keeps ids human-readable.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "feed").lower()).strip("-") or "feed"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


# --- Persistence ---------------------------------------------------------------


def _lock():
    return FileLock(f"{SUBSCRIPTIONS_FILE}.lock", timeout=10)


def _read_file() -> list[dict]:
    try:
        with open(SUBSCRIPTIONS_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Could not read %s (%s); treating the pull list as empty.",
                     SUBSCRIPTIONS_FILE, exc)
        return []
    entries = data.get("subscriptions", []) if isinstance(data, dict) else data
    return [e for e in entries if isinstance(e, dict) and e.get("url")]


def _write_file(entries: list[dict]) -> None:
    os.makedirs(os.path.dirname(SUBSCRIPTIONS_FILE) or ".", exist_ok=True)
    tmp = f"{SUBSCRIPTIONS_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"subscriptions": entries}, handle, indent=2)
    os.replace(tmp, SUBSCRIPTIONS_FILE)


def load() -> list[dict]:
    """Return the persisted pull list (no lock: readers tolerate a stale view)."""
    return _read_file()


def _mutate(fn):
    """Apply fn to the pull list under the file lock and persist the result."""
    with _lock():
        entries = _read_file()
        result, entries = fn(entries)
        _write_file(entries)
    return result


def seed_from_env() -> None:
    """Merge SUBSCRIBED_ICS (a JSON name->url map) into the persisted pull list.

    Never raises: a malformed seed is logged and the server starts anyway with
    whatever is already persisted.
    """
    if not SUBSCRIBED_ICS.strip():
        return
    try:
        seed = json.loads(SUBSCRIBED_ICS)
        if not isinstance(seed, dict):
            raise ValueError("SUBSCRIBED_ICS must be a JSON object of name -> url.")
    except Exception as exc:
        logger.error("Ignoring SUBSCRIBED_ICS — %s: %s", type(exc).__name__, exc)
        return

    added = []
    for name, url in seed.items():
        try:
            normalized = normalize_url(str(url))
        except ValueError as exc:
            logger.error("Ignoring seeded feed %r — %s", name, exc)
            continue
        # Seeding does not fetch: startup must not block on a slow/dead feed.
        if upsert(str(name), normalized) == "added":
            added.append(name)
    if added:
        logger.info("Seeded %d subscription(s) from SUBSCRIBED_ICS: %s",
                    len(added), ", ".join(added))


def upsert(name: str, url: str) -> str:
    """Add or refresh an entry keyed by normalized URL. Returns 'added'/'updated'."""

    def apply(entries: list[dict]):
        for entry in entries:
            if entry["url"] == url:
                entry["name"] = name or entry.get("name", "")
                entry["id"] = make_id(entry["name"], url)
                return "updated", entries
        entries.append(
            {
                "id": make_id(name, url),
                "name": name,
                "url": url,
                "read_only": True,
                "added_at": datetime.now(timezone.utc).isoformat(),
                "last_fetch": None,
                "last_status": None,
            }
        )
        return "added", entries

    return _mutate(apply)


def remove(id_or_url: str) -> dict | None:
    """Remove an entry by id or URL. Returns the removed entry, or None."""
    target = (id_or_url or "").strip()
    try:
        normalized = normalize_url(target)
    except ValueError:
        normalized = None

    def apply(entries: list[dict]):
        for i, entry in enumerate(entries):
            if entry.get("id") == target or entry["url"] in (target, normalized):
                return entries.pop(i), entries
        return None, entries

    return _mutate(apply)


def record_status(url: str, status: str) -> None:
    """Best-effort stamp of last_fetch/last_status on an entry.

    A no-op when no entry has this URL yet, which is the case during the
    validating fetch of a brand-new feed — so callers that add a feed stamp it
    again after the upsert, otherwise a freshly added feed would report as never
    fetched until something happened to read it.
    """

    def apply(entries: list[dict]):
        for entry in entries:
            if entry["url"] == url:
                entry["last_fetch"] = datetime.now(timezone.utc).isoformat()
                entry["last_status"] = status
        return None, entries

    try:
        _mutate(apply)
    except Exception as exc:  # never fail a read because bookkeeping failed
        logger.warning("Could not record fetch status for %s: %s", url, exc)


# --- Fetch + parse -------------------------------------------------------------


def is_permitted(entry: dict) -> bool:
    """Whether ALLOWED_SUBSCRIPTIONS (if set) permits reading this feed."""
    if not ALLOWED_SUBSCRIPTIONS:
        return True
    return entry.get("id") in ALLOWED_SUBSCRIPTIONS or entry.get("name") in ALLOWED_SUBSCRIPTIONS


def resolve_exact(target: str) -> dict | None:
    """Find a subscription by id or feed URL — its unambiguous identity.

    Safe to check before consulting CalDAV: an id or feed URL can only ever mean
    a subscription.
    """
    candidate = (target or "").strip()
    if not candidate:
        return None
    entries = load()
    for entry in entries:
        if entry.get("id") == candidate:
            return entry
    try:
        normalized = normalize_url(candidate)
    except ValueError:
        normalized = None
    if normalized:
        for entry in entries:
            if entry["url"].rstrip("/") == normalized.rstrip("/"):
                return entry
    return None


def resolve_by_name(target: str) -> dict | None:
    """Find a subscription by display name.

    Names are neither unique nor reserved — a feed can be named the same as a
    real calendar — so callers must try real calendars first and fall back to
    this, otherwise adding a feed called "Home" would silently shadow the actual
    Home calendar.
    """
    candidate = (target or "").strip()
    if not candidate:
        return None
    for entry in load():
        if entry.get("name") == candidate:
            return entry
    return None


def resolve(target: str) -> dict | None:
    """Find a subscription by id, URL, or name (in that order of confidence)."""
    return resolve_exact(target) or resolve_by_name(target)


def fetch(url: str, force: bool = False) -> str:
    """Return the feed body, using a short-lived cache and conditional GETs.

    Feeds are large and change rarely, so a cached body is reused for
    ICS_CACHE_TTL seconds and then revalidated with ETag/If-Modified-Since rather
    than re-downloaded. Redirects are followed manually so every hop can be
    re-checked against the SSRF guard.
    """
    with _cache_lock:
        cached = _cache.get(url)
    now = datetime.now(timezone.utc).timestamp()
    if cached and not force and now - cached["fetched_at"] < ICS_CACHE_TTL:
        return cached["text"]

    headers = {"User-Agent": "caldav-mcp/1.0 (+ICS subscription)"}
    if cached:
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]

    current = url
    try:
        with httpx.Client(timeout=ICS_TIMEOUT, follow_redirects=False) as client:
            for _ in range(_MAX_REDIRECTS):
                _assert_public_host(current)
                response = client.get(current, headers=headers)
                # Match redirect codes explicitly rather than httpx's is_redirect:
                # 304 Not Modified is also a 3xx, and treating it as a redirect
                # would break the conditional-GET revalidation below.
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError(
                            f"HTTP {response.status_code} redirect without a Location header."
                        )
                    current = str(response.url.join(location))
                    continue
                break
            else:
                raise RuntimeError(f"Too many redirects (>{_MAX_REDIRECTS}).")
    except ValueError:
        raise
    except Exception as exc:
        record_status(url, f"error: {type(exc).__name__}: {exc}")
        raise RuntimeError(f"Could not fetch feed {url}: {type(exc).__name__}: {exc}") from exc

    if response.status_code == 304 and cached:
        with _cache_lock:
            _cache[url]["fetched_at"] = now
        record_status(url, "ok (304 not modified)")
        return cached["text"]

    if response.status_code >= 400:
        record_status(url, f"error: HTTP {response.status_code}")
        raise RuntimeError(f"Feed {url} returned HTTP {response.status_code}.")

    text = response.text
    with _cache_lock:
        _cache[url] = {
            "text": text,
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
            "fetched_at": now,
        }
    record_status(url, f"ok (HTTP {response.status_code}, {len(text)} bytes)")
    return text


def parse(text: str) -> ICalendar:
    """Parse a feed body, raising a readable error when it isn't iCalendar."""
    try:
        cal = ICalendar.from_ical(text)
    except Exception as exc:
        raise ValueError(
            f"Feed did not parse as iCalendar ({type(exc).__name__}: {exc})."
        ) from exc
    if cal.name != "VCALENDAR":
        raise ValueError(f"Feed root component is {cal.name!r}, expected VCALENDAR.")
    return cal


def expand_events(entry: dict, start_dt, end_dt) -> list:
    """Return VEVENT occurrences in [start, end), expanding RRULEs.

    Team/league feeds lean on RRULE, so a naive walk over the VEVENTs would
    report a weekly practice once instead of on every date in the window.
    """
    cal = parse(fetch(entry["url"]))
    return recurring_ical_events.of(cal).between(start_dt, end_dt)


def find_event(entry: dict, uid: str):
    """Return the first VEVENT in the feed with this UID, or None.

    Unlike expand_events this scans the whole feed (no window), so it finds an
    event regardless of when it occurs.
    """
    cal = parse(fetch(entry["url"]))
    for component in cal.walk("VEVENT"):
        if str(component.get("uid", "")) == uid:
            return component
    return None


# --- CLI -----------------------------------------------------------------------


def _cli(argv: list[str] | None = None) -> int:
    """Manage the pull list from the command line, as a backup to the MCP tools.

    The MCP tools are the normal path, but they are only reachable when the
    server and the proxy in front of it are healthy. This runs against the same
    persisted file under the same lock, so it works while the server is running
    and while the connector is down:

        docker compose exec -T caldav-mcp python subscriptions.py list
        docker compose exec -T caldav-mcp python subscriptions.py add "Name" "webcal://..."
        docker compose exec -T caldav-mcp python subscriptions.py remove <id-or-url>

    Changes take effect immediately: the pull list is read per call, so no
    restart is needed.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="subscriptions",
        description="Manage the ICS subscription pull list (backup for the MCP tools).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Show the pull list as JSON.")
    p_add = sub.add_parser("add", help="Subscribe to an ICS feed.")
    p_add.add_argument("name")
    p_add.add_argument("url")
    p_add.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip the validating fetch (use when the feed is temporarily unreachable).",
    )
    p_remove = sub.add_parser("remove", help="Remove a feed by id or URL.")
    p_remove.add_argument("id_or_url")
    args = parser.parse_args(argv)

    if args.command == "list":
        print(json.dumps(load(), indent=2))
        return 0

    if args.command == "add":
        try:
            normalized = normalize_url(args.url)
            if not args.no_validate:
                parse(fetch(normalized, force=True))
        except Exception as exc:
            print(f"error: {exc}")
            return 1
        action = upsert(args.name, normalized)
        if not args.no_validate:
            # The validating fetch ran before the entry existed; stamp it now.
            record_status(normalized, "ok (validated on add)")
        entry = resolve_exact(normalized)
        print(f"{action}: {entry['id']}  {args.name}  {normalized}")
        return 0

    removed = remove(args.id_or_url)
    if removed is None:
        print(f"no subscription matched {args.id_or_url!r}")
        return 1
    print(f"removed: {removed['id']}  {removed.get('name', '')}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(_cli())
