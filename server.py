"""MCP server exposing CalDAV read/write tools (iCloud-ready).

The server implements NO authentication of its own by design. It is meant to run
on an internal network, fronted by an identity-aware authorization proxy (e.g.
Pomerium in MCP mode) that authenticates and authorizes every request before it
reaches `/mcp`. See README.md.

Configuration is entirely via environment variables (see .env.example). Point it
at Apple's iCloud CalDAV endpoint with your Apple ID and an app-specific password
(https://caldav.icloud.com/), or at any other CalDAV server.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal, NotRequired, TypedDict

import caldav
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent
from icalendar import vCalAddress

import carddav
import subscriptions
from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import PlainTextResponse

logger = logging.getLogger("caldav-mcp")


AttendeeRole = Literal["CHAIR", "REQ-PARTICIPANT", "OPT-PARTICIPANT", "NON-PARTICIPANT"]
AttendeePartstat = Literal[
    "NEEDS-ACTION", "ACCEPTED", "DECLINED", "TENTATIVE", "DELEGATED"
]


class EventAttendee(TypedDict):
    email: str
    name: NotRequired[str]
    role: NotRequired[AttendeeRole]
    partstat: NotRequired[AttendeePartstat]
    rsvp: NotRequired[bool]


class EventOrganizer(TypedDict):
    email: str
    name: NotRequired[str]


class ContactValue(TypedDict):
    value: str
    type: NotRequired[str]
    params: NotRequired[dict[str, str | list[str]]]


class ContactAddress(TypedDict):
    type: NotRequired[str]
    params: NotRequired[dict[str, str | list[str]]]
    street: NotRequired[str]
    city: NotRequired[str]
    region: NotRequired[str]
    code: NotRequired[str]
    country: NotRequired[str]

# --- Configuration (all from env; secrets injected at runtime, never baked in) ---
# iCloud's CalDAV entry point. The client performs principal/calendar discovery
# from here, following redirects to the account's partition host.
CALDAV_URL = os.environ.get("CALDAV_URL", "https://caldav.icloud.com/")
# For iCloud this is your Apple ID (full email address).
CALDAV_USERNAME = os.environ.get("CALDAV_USERNAME", "")
# Optional EGroupware calendar owner; authentication still uses CALDAV_USERNAME.
CALDAV_CALENDAR_USER = os.environ.get("CALDAV_CALENDAR_USER", "").strip()
# For iCloud this is an app-specific password
# (Apple ID -> Sign-In and Security -> App-Specific Passwords), NOT your login.
CALDAV_PASSWORD = os.environ.get("CALDAV_PASSWORD", "")
# Optional CardDAV address-book owner; authentication still uses
# CALDAV_USERNAME. Independent of CALDAV_CALENDAR_USER: a deployment may target
# someone else's calendar and someone else's (or their own) address book.
# Empty/unset falls back to CALDAV_USERNAME — see _contacts_user().
CARDDAV_CONTACTS_USER = os.environ.get("CARDDAV_CONTACTS_USER", "").strip()
# Calendar used when a tool call omits `calendar` (matched by display name).
DEFAULT_CALENDAR = os.environ.get("DEFAULT_CALENDAR", "")
# Comma-separated allowlist of calendar display names. When set, tools may only
# read from / write to these calendars — even a misused tool cannot touch others.
# Leave empty to allow every calendar in the account.
ALLOWED_CALENDARS = [
    c.strip() for c in os.environ.get("ALLOWED_CALENDARS", "").split(",") if c.strip()
]
# When true, writing tools (create/update/delete) are refused — a read-only mode.
READ_ONLY = os.environ.get("READ_ONLY", "false").lower() == "true"
# Per-request network timeout (seconds) for CalDAV calls. Without this the client
# waits indefinitely on a slow iCloud REPORT, so the fronting proxy eventually
# kills the connection and the caller sees a 502 instead of a clean tool error.
# Keep it comfortably below the proxy's gateway timeout.
CALDAV_TIMEOUT = int(os.environ.get("CALDAV_TIMEOUT", "20"))
# Expand recurring events server-side so each occurrence appears on its real date
# within the window. iCloud's expand support is uneven; list_events falls back to
# an unexpanded query when expansion errors. Set to `false` to skip expand entirely.
EXPAND_RECURRENCES = os.environ.get("EXPAND_RECURRENCES", "true").lower() == "true"
# How far either side of today the UID fallback scan looks before widening to the
# whole calendar — see _find_event(). Only used when the server cannot answer a
# UID-filtered REPORT (iCloud); a compliant server never gets this far. Most
# lookups target a recent or upcoming event, so a year each way resolves them in
# one query while keeping the response small.
UID_SCAN_WINDOW_DAYS = int(os.environ.get("UID_SCAN_WINDOW_DAYS", "366"))
# Outer bounds for the widened sweep that runs when the window above misses. Same
# range python-caldav uses for its own unbounded-search workaround: wide enough to
# cover any real calendar entry, bounded because several CalDAV servers reject a
# time-range with no start or end at all.
UID_SWEEP_START = datetime(1970, 1, 1, tzinfo=timezone.utc)
UID_SWEEP_END = datetime(2126, 1, 1, tzinfo=timezone.utc)

# Optional app-layer backstop. The external proxy is still REQUIRED regardless.
# When enabled, /mcp requests must carry a Pomerium identity assertion whose JWT
# is cryptographically verified (signature + exp + audience) against Pomerium's
# JWKS — this blocks anything on the shared network that tries to reach the app
# directly, bypassing Pomerium.
REQUIRE_POMERIUM_IDENTITY = os.environ.get("REQUIRE_POMERIUM_IDENTITY", "false").lower() == "true"
# Candidate header(s) carrying the assertion JWT. Pomerium's MCP mode uses
# `x-pomerium-assertion`; the general identity header is `x-pomerium-jwt-assertion`.
POMERIUM_IDENTITY_HEADER = os.environ.get(
    "POMERIUM_IDENTITY_HEADER", "x-pomerium-assertion,x-pomerium-jwt-assertion"
)
POMERIUM_ASSERTION_HEADERS = [
    h.strip().lower() for h in POMERIUM_IDENTITY_HEADER.split(",") if h.strip()
]
# Pomerium's JWKS endpoint (its signing key's public keys), e.g.
# https://<route-host>/.well-known/pomerium/jwks.json. Required when the gate is on.
POMERIUM_JWKS_URL = os.environ.get("POMERIUM_JWKS_URL", "")
# Expected `aud`/`iss` claims. `aud` is the route's upstream URL/host; verified
# when set. `iss` verified only when set.
POMERIUM_AUDIENCE = os.environ.get("POMERIUM_AUDIENCE", "")
POMERIUM_ISSUER = os.environ.get("POMERIUM_ISSUER", "")

# Connect to CalDAV on startup to verify the configuration. On failure the error
# is logged and the server keeps running.
STARTUP_TEST = os.environ.get("STARTUP_TEST", "false").lower() == "true"

# The container healthcheck polls /healthz every 30s, so its access-log lines
# drown out everything that actually happened — a tool call, a failed fetch. They
# are filtered out by default; set this to `true` when debugging the probe itself.
LOG_HEALTHZ = os.environ.get("LOG_HEALTHZ", "false").lower() == "true"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

# Host/Origin allowlist for the SDK's DNS-rebinding guard (MCP SDK >= 2). The
# guard compares the request's `Host` header against this list and answers 421
# when it does not match. Behind Pomerium the header is the *public route host*
# (e.g. caldav-mcp.example.com), not the container's bind address, so the guard
# has to be told about it — see _transport_security() for what happens when this
# is left empty. Entries are `host:port` patterns; `example.com:*` allows any port.
MCP_ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
]
# Matching allowlist for the `Origin` header, for browser-based clients. Defaults
# to https:// + each allowed host when left empty but MCP_ALLOWED_HOSTS is set.
MCP_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
]

# How long (ms) a client may reuse a cached `tools/list` result. The catalog is
# fixed at import time (calendar/subscription/contacts tools): it cannot change
# while the process runs, so the only thing that invalidates it is a restart on
# a new image.
TOOLS_LIST_TTL_MS = int(os.environ.get("TOOLS_LIST_TTL_MS", str(60 * 60 * 1000)))

mcp = MCPServer(
    "caldav-mcp",
    title="CalDAV Calendar",
    website_url="https://github.com/JB09/caldav-mcp-wrapper",
    # `cacheScope: public` (MCP 2026-07-28) says a cached result may be shared
    # across authorization contexts. That is true here and worth being explicit
    # about, because it is the risky half of the setting: this server does not
    # vary its catalog by caller — READ_ONLY gates *execution* in
    # _require_writable(), it does not hide the write tools from `tools/list` —
    # so no identity-specific data can leak through a shared cache entry. If a
    # tool is ever registered conditionally on who is asking, this must become
    # "private".
    cache_hints={"tools/list": CacheHint(ttl_ms=TOOLS_LIST_TTL_MS, scope="public")},
)


class _HealthzFilter(logging.Filter):
    """Drop uvicorn access-log lines for the healthcheck endpoint."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


def _quiet_healthz_logging() -> None:
    """Filter healthcheck noise out of the access log.

    Applied after uvicorn has configured its loggers, since uvicorn's dictConfig
    would otherwise replace the logger this attaches to.
    """
    if not LOG_HEALTHZ:
        logging.getLogger("uvicorn.access").addFilter(_HealthzFilter())


def _get_principal() -> "caldav.Principal":
    """Open a fresh CalDAV session and return its principal.

    A new DAVClient is built per call rather than cached for the process
    lifetime. A long-lived client's pooled HTTPS connection to iCloud can go
    stale, so a later REPORT (e.g. a calendar-query for list_events) hangs until
    the read timeout even though a fresh connection answers in a fraction of a
    second. Discovery is cheap, so this trades a negligible per-call cost for
    robustness — and avoids sharing one requests.Session across the server's
    worker threads.

    Raises RuntimeError if credentials are missing (before any network I/O).
    """
    if not (CALDAV_USERNAME and CALDAV_PASSWORD):
        raise RuntimeError(
            "CALDAV_USERNAME and CALDAV_PASSWORD must be configured to reach CalDAV."
        )
    client = caldav.DAVClient(
        url=CALDAV_URL,
        username=CALDAV_USERNAME,
        password=CALDAV_PASSWORD,
        timeout=CALDAV_TIMEOUT,
    )
    return client.principal()


def _calendar_user() -> str:
    """Return the calendar owner, falling back to the authenticated user."""
    return CALDAV_CALENDAR_USER or CALDAV_USERNAME


def _egroupware_calendar_url() -> str:
    """Build the direct EGroupware calendar URL for the configured owner."""
    from urllib.parse import quote, urlsplit, urlunsplit

    base = urlsplit(CALDAV_URL)
    base_path = "/" + "/".join(part for part in base.path.split("/") if part)
    path = f"{base_path.rstrip('/')}/{quote(_calendar_user(), safe='')}/calendar/"
    return urlunsplit((base.scheme, base.netloc, path, base.query, base.fragment))


def _principal_calendars(principal: "caldav.Principal") -> list["caldav.Calendar"]:
    """Return calendars for the selected owner, using direct EGroupware access when set."""
    if CALDAV_CALENDAR_USER:
        return [principal.client.calendar(url=_egroupware_calendar_url())]
    return principal.calendars()


# --- Contacts / CardDAV --------------------------------------------------------
#
# Mirrors the calendar owner-selection above: CALDAV_USERNAME always
# authenticates; CARDDAV_CONTACTS_USER (independent of CALDAV_CALENDAR_USER)
# selects whose address book is targeted, defaulting to the authenticated user.
# The heavy lifting (REPORT/PROPFIND bodies, vCard parsing) lives in carddav.py;
# this server only resolves owner/URL and wires the MCP tools to it.


def _contacts_user() -> str:
    """Return the address-book owner, falling back to the authenticated user."""
    return CARDDAV_CONTACTS_USER or CALDAV_USERNAME


def _get_carddav_client() -> "caldav.DAVClient":
    """Open a fresh CardDAV session (same credentials as _get_principal()).

    A new DAVClient per call for the same staleness reason _get_principal()
    documents. No principal()/discovery round-trip is needed here: the
    EGroupware address-book layout is predictable from the owner alone (see
    _egroupware_addressbook_home_url()), exactly like the direct calendar path.
    """
    if not (CALDAV_USERNAME and CALDAV_PASSWORD):
        raise RuntimeError(
            "CALDAV_USERNAME and CALDAV_PASSWORD must be configured to reach CardDAV."
        )
    return caldav.DAVClient(
        url=CALDAV_URL,
        username=CALDAV_USERNAME,
        password=CALDAV_PASSWORD,
        timeout=CALDAV_TIMEOUT,
    )


def _egroupware_addressbook_home_url() -> str:
    """Build the EGroupware home-set URL for the configured contacts owner.

    This is the parent of the owner's address book(s) — `.../<user>/` — used to
    discover available address books (`list_contact_books`), by direct analogy
    with `_egroupware_calendar_url()`'s `.../<user>/calendar/`.
    """
    from urllib.parse import quote, urlsplit, urlunsplit

    base = urlsplit(CALDAV_URL)
    base_path = "/" + "/".join(part for part in base.path.split("/") if part)
    path = f"{base_path.rstrip('/')}/{quote(_contacts_user(), safe='')}/"
    return urlunsplit((base.scheme, base.netloc, path, base.query, base.fragment))


def _egroupware_addressbook_url() -> str:
    """Build the direct EGroupware default address-book URL for the owner."""
    return f"{_egroupware_addressbook_home_url().rstrip('/')}/addressbook/"


def _resolve_addressbook_url(address_book: str | None) -> str:
    """Resolve the `address_book` tool argument to a collection URL.

    Accepts a URL (as returned by `list_contact_books`) directly, a display
    name matched against `list_contact_books`, or — when omitted — the default
    address book for the configured contacts owner.
    """
    target = (address_book or "").strip()
    if not target:
        return _egroupware_addressbook_url()
    if target.startswith(("http://", "https://")):
        return target
    client = _get_carddav_client()
    for book in carddav.discover_addressbooks(client, _egroupware_addressbook_home_url()):
        if book["name"] == target:
            return book["url"]
    raise ValueError(f"Address book {target!r} was not found for {_contacts_user()!r}.")


def _require_contact_writable() -> None:
    """Guard mutating contact tools when the server is configured read-only."""
    if READ_ONLY:
        raise RuntimeError("Server is in READ_ONLY mode; writing tools are disabled.")


def _scan_contacts_for_uid(client, addressbook_url: str, uid: str) -> dict | None:
    """Scan every vCard in the address book for one matching `uid`, or `None`."""
    for entry in carddav.fetch_all_vcards(client, addressbook_url):
        contact = carddav.vcard_to_contact(entry["text"])
        if contact.get("uid") == uid:
            return entry
    return None


def _find_contact(client, addressbook_url: str, uid: str) -> dict:
    """Resolve a contact UID to its `{"href", "etag", "text"}`, or raise.

    Tries the server-side REPORT first; falls back to scanning every vCard in
    the address book, the same graceful-degradation shape `_find_event` uses
    for calendars that reject a UID-filtered REPORT.
    """
    try:
        found = carddav.find_vcard_by_uid_server_side(client, addressbook_url, uid)
        if found is not None:
            return found
    except Exception as exc:
        logger.debug(
            "Server-side UID lookup for %r failed (%s: %s); scanning the address "
            "book instead.",
            uid,
            type(exc).__name__,
            exc,
        )
        found = _scan_contacts_for_uid(client, addressbook_url, uid)
        if found is not None:
            return found
        raise caldav.error.NotFoundError(
            f"No contact with UID {uid!r} in {addressbook_url!r}."
        ) from exc
    found = _scan_contacts_for_uid(client, addressbook_url, uid)
    if found is not None:
        return found
    raise caldav.error.NotFoundError(f"No contact with UID {uid!r} in {addressbook_url!r}.")


def _calendar_name(cal: "caldav.Calendar") -> str:
    """Return a calendar's display name across caldav versions.

    caldav 3.x deprecated the `.name` attribute in favour of
    `get_display_name()`; fall back to `.name` on older releases.
    """
    getter = getattr(cal, "get_display_name", None)
    if getter is not None:
        return getter() or ""
    return cal.name or ""


def _supported_components(cal: "caldav.Calendar") -> list[str]:
    """Return the collection's advertised component types, e.g. ['VEVENT'] for an
    event calendar or ['VTODO'] for a Reminders/task list.

    iCloud (and CalDAV generally) exposes Reminders lists as collections
    alongside calendars; this is how they are told apart. Best-effort: returns []
    when the server does not advertise a `supported-calendar-component-set`.
    """
    try:
        comps = cal.get_supported_components(with_fallback=False)
    except Exception:
        return []
    return [str(c) for c in comps] if comps else []


def _calendar_kind(components: list[str]) -> str:
    """Classify a collection from its component set: event calendar vs task list."""
    if "VEVENT" in components:
        return "calendar"
    if "VTODO" in components:
        return "tasks"
    return "unknown"


def _resolve_calendar(name: str | None, component: str = "VEVENT") -> "caldav.Calendar":
    """Resolve a calendar by display name *or* URL, enforcing the allowlist.

    `target` may be a calendar URL (as returned by `list_calendars`) — this
    disambiguates accounts with duplicate display names (e.g. two "Family"
    calendars). Otherwise it is matched by display name. The allowlist is checked
    against the resolved calendar's name. Raises ValueError when no calendar is
    selected/found or it is not permitted — all *before* any mutating call.

    A display name can be shared by collections of *different kinds*: an iCloud
    account with a "Family" calendar and a "Family" Reminders list returns both
    here, and the Reminders list may come first. Matching purely on name then
    resolves an event query to a VTODO collection, which answers `[]` — no
    error, indistinguishable from an empty week. So a name match prefers a
    collection that actually advertises `component`, and only falls back to the
    first name match when none does (a server that does not advertise its
    component set at all must still resolve).
    """
    target = (name or DEFAULT_CALENDAR).strip()
    if not target:
        raise ValueError("No calendar: pass `calendar` or set DEFAULT_CALENDAR.")

    principal = _get_principal()
    calendars = _principal_calendars(principal)
    match = None
    if target.startswith(("http://", "https://")):
        for cal in calendars:
            if str(cal.url).rstrip("/") == target.rstrip("/"):
                match = cal
                break
    elif CALDAV_CALENDAR_USER:
        calendar = calendars[0]
        if _calendar_name(calendar) == target:
            match = calendar
    if match is None:
        named = [cal for cal in calendars if _calendar_name(cal) == target]
        # Prefer a collection of the right kind; fall back to the first by name.
        match = next(
            (cal for cal in named if component in _supported_components(cal)),
            named[0] if named else None,
        )
    if match is None:
        raise ValueError(f"Calendar {target!r} was not found in the account.")

    # Calendar hard-limit: even a misused tool cannot touch calendars off the list.
    resolved = _calendar_name(match)
    if ALLOWED_CALENDARS and resolved not in ALLOWED_CALENDARS:
        raise ValueError(
            f"Calendar {resolved!r} is not permitted. "
            f"Allowed calendars: {', '.join(ALLOWED_CALENDARS)}."
        )
    return match


def _resolve_target(name: str | None) -> str:
    """Return the calendar/subscription a tool call refers to, applying the default."""
    target = (name or DEFAULT_CALENDAR).strip()
    if not target:
        raise ValueError("No calendar: pass `calendar` or set DEFAULT_CALENDAR.")
    return target


def _permitted_subscription(entry: dict) -> dict:
    """Return the entry, or raise if ALLOWED_SUBSCRIPTIONS excludes it.

    Raising (rather than treating it as "no match") makes a denied feed report
    why, instead of falling through to a confusing "calendar not found".
    """
    if not subscriptions.is_permitted(entry):
        raise ValueError(
            f"Subscription {entry.get('name') or entry['id']!r} is not permitted. "
            f"Allowed subscriptions: {', '.join(subscriptions.ALLOWED_SUBSCRIPTIONS)}."
        )
    return entry


def _resolve_any(target: str) -> tuple[str, object]:
    """Resolve a target to ("subscription", entry) or ("calendar", cal).

    Real calendars win on a name match: a subscription id/feed URL can only mean
    a subscription, but a *name* can belong to either, and a feed must never
    shadow the account's own calendar (adding a feed called "Home" would
    otherwise silently redirect every read away from the real Home calendar).
    So: exact subscription identity, then real calendars, then feed names.
    """
    entry = subscriptions.resolve_exact(target)
    if entry is not None:
        return "subscription", _permitted_subscription(entry)
    try:
        return "calendar", _resolve_calendar(target)
    except Exception as exc:
        # Fall back to a feed of this name. This catches more than "no such
        # calendar": if CalDAV is unreachable or misconfigured, subscriptions do
        # not depend on it and stay readable, which is much of their value when
        # iCloud is having a bad day. The name may in principle belong to a real
        # calendar we could not reach, so say so rather than failing silently.
        entry = subscriptions.resolve_by_name(target)
        if entry is None:
            raise
        if not isinstance(exc, ValueError):
            logger.warning(
                "CalDAV lookup for %r failed (%s: %s); serving the subscription of "
                "that name instead.",
                target,
                type(exc).__name__,
                exc,
            )
        return "subscription", _permitted_subscription(entry)


def _resolve_writable(target: str) -> "caldav.Calendar":
    """Resolve a target for a mutating tool, refusing read-only ICS subscriptions."""
    kind, resolved = _resolve_any(target)
    if kind == "subscription":
        raise ValueError(
            f"Calendar {target!r} is a read-only ICS subscription "
            f"(id {resolved['id']}); it cannot be created in, updated, or deleted from."
        )
    return resolved


def _require_writable() -> None:
    """Guard mutating tools when the server is configured read-only."""
    if READ_ONLY:
        raise RuntimeError("Server is in READ_ONLY mode; writing tools are disabled.")


def _parse_dt(value: str, all_day: bool) -> date | datetime:
    """Parse an ISO 8601 string into a date (all-day) or timezone-aware datetime.

    All-day events use a bare date (`YYYY-MM-DD`). Timed events accept full ISO
    timestamps; a naive value is assumed to be UTC.
    """
    if all_day:
        return date.fromisoformat(value[:10])
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _isoformat(value) -> str | None:
    """Best-effort ISO string for a date/datetime property value."""
    if value is None:
        return None
    dt = getattr(value, "dt", value)
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)


def _summarize_component(comp) -> dict:
    """Convert a VEVENT to JSON without hiding attendee or extension properties."""
    result = {
        "uid": str(comp.get("uid", "")),
        "summary": str(comp.get("summary", "")),
        "start": _isoformat(comp.get("dtstart")),
        "end": _isoformat(comp.get("dtend")),
        "location": str(comp["location"]) if "location" in comp else None,
        "description": str(comp["description"]) if "description" in comp else None,
        "status": str(comp["status"]) if "status" in comp else None,
        "class": str(comp["class"]) if "class" in comp else None,
        "transp": str(comp["transp"]) if "transp" in comp else None,
        "categories": [str(v) for v in comp["categories"].cats] if "categories" in comp else [],
        "url": str(comp["url"]) if "url" in comp else None,
        "priority": int(comp["priority"]) if "priority" in comp else None,
        "contact": str(comp["contact"]) if "contact" in comp else None,
        "rrule": _json_safe(dict(comp["rrule"])) if "rrule" in comp else None,
        "attendees": [_attendee_dict(a) for a in _ical_values(comp, "attendee")],
        "organizer": _organizer_dict(comp["organizer"]) if "organizer" in comp else None,
    }
    result["custom_fields"] = _custom_ical_fields(comp)
    return result


def _address_email(value) -> str:
    text = str(value)
    return text[7:] if text.lower().startswith("mailto:") else text


def _ical_values(component, name: str) -> list:
    value = component.get(name)
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _attendee_dict(value) -> dict:
    params = getattr(value, "params", {})
    result = {"email": _address_email(value)}
    result["params"] = {str(k): _json_safe(v) for k, v in params.items()}
    for key in ("CN", "ROLE", "PARTSTAT", "RSVP", "CUTYPE",
                "DELEGATED-FROM", "DELEGATED-TO", "MEMBER", "SCHEDULE-STATUS"):
        if key in params:
            field = {
                "CN": "name",
                "DELEGATED-FROM": "delegated_from",
                "DELEGATED-TO": "delegated_to",
                "SCHEDULE-STATUS": "schedule_status",
            }.get(key, key.lower())
            result[field] = (
                str(params[key]).upper() == "TRUE"
                if key == "RSVP"
                else _json_safe(params[key])
            )
    return result


def _organizer_dict(value) -> dict:
    params = getattr(value, "params", {})
    result = {"email": _address_email(value)}
    if "CN" in params:
        result["name"] = str(params["CN"])
    result["params"] = {str(k): str(v) for k, v in params.items()}
    return result


def _custom_ical_fields(component) -> dict:
    return {
        name: [_json_safe(value) for value in _ical_values(component, name)]
        if len(_ical_values(component, name)) > 1
        else _json_safe(_ical_values(component, name)[0])
        for name in component.keys()
        if str(name).upper() not in _EVENT_MAPPED_PROPERTIES
    }


def _set_ical_property(component, name: str, value) -> None:
    if name in component:
        del component[name]
    if value is None:
        return
    component.add(name, value)


_ICAL_PROPERTY_NAME = re.compile(r"^[A-Z0-9-]+$")
_EVENT_MAPPED_PROPERTIES = {
    "UID", "SUMMARY", "DTSTART", "DTEND", "LOCATION", "DESCRIPTION", "STATUS",
    "CLASS", "TRANSP", "CATEGORIES", "URL", "PRIORITY", "CONTACT", "RRULE",
    "ATTENDEE", "ORGANIZER",
}
_EVENT_CUSTOM_PROTECTED_PROPERTIES = {
    "UID", "DTSTART", "DTEND", "SUMMARY", "DESCRIPTION", "LOCATION", "STATUS",
    "CLASS", "TRANSP", "CATEGORIES", "URL", "PRIORITY", "CONTACT", "RRULE",
    "ATTENDEE", "ORGANIZER", "BEGIN", "END", "VERSION",
}


def _set_custom_ical_fields(component, fields: dict | None) -> None:
    for raw_name, value in (fields or {}).items():
        name = str(raw_name).upper()
        if not _ICAL_PROPERTY_NAME.fullmatch(name) or name in _EVENT_CUSTOM_PROTECTED_PROPERTIES:
            raise ValueError(f"Custom iCalendar property name {raw_name!r} is invalid or has a dedicated field.")
        if name in component:
            del component[name]
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            component.add(name, item)


_PARTSTAT_VALUES = {"NEEDS-ACTION", "ACCEPTED", "DECLINED", "TENTATIVE", "DELEGATED"}
_ROLE_VALUES = {"CHAIR", "REQ-PARTICIPANT", "OPT-PARTICIPANT", "NON-PARTICIPANT"}


def _new_attendee(attendee: EventAttendee) -> vCalAddress:
    email = (attendee.get("email") or "").strip()
    email = email[7:] if email.lower().startswith("mailto:") else email
    if not email or "@" not in email:
        raise ValueError("An attendee email address is required.")
    value = vCalAddress(f"mailto:{email}")
    params = value.params
    if attendee.get("name"):
        params["CN"] = attendee["name"]
    if attendee.get("role"):
        role = attendee["role"].upper()
        if role not in _ROLE_VALUES:
            raise ValueError(f"Invalid iCalendar attendee ROLE {role!r}.")
        params["ROLE"] = role
    if attendee.get("partstat"):
        partstat = attendee["partstat"].upper()
        if partstat not in _PARTSTAT_VALUES:
            raise ValueError(f"Invalid iCalendar attendee PARTSTAT {partstat!r}.")
        params["PARTSTAT"] = partstat
    if attendee.get("rsvp") is not None:
        params["RSVP"] = "TRUE" if attendee["rsvp"] else "FALSE"
    return value


def _add_attendees(vevent, attendees: list[EventAttendee] | None) -> None:
    for attendee in attendees or []:
        value = _new_attendee(attendee)
        existing = _find_attendee(vevent, attendee.get("email", ""))
        if existing is None:
            vevent.add("attendee", value)
        else:
            existing.params.update(value.params)


def _find_attendee(vevent, email: str):
    target = email.casefold().removeprefix("mailto:")
    return next(
        (a for a in _ical_values(vevent, "attendee") if _address_email(a).casefold() == target),
        None,
    )


def _set_organizer(vevent, organizer: EventOrganizer | None) -> None:
    if organizer is None:
        return
    value = _new_attendee({"email": organizer.get("email", ""), "name": organizer.get("name")})
    # Keep existing ORGANIZER parameters not explicitly supplied by this schema.
    if "organizer" in vevent:
        previous = vevent["organizer"]
        for key, param in previous.params.items():
            if key not in value.params:
                value.params[key] = param
    _set_ical_property(vevent, "organizer", value)


def _summarize_event(event: "caldav.Event") -> dict:
    """Summarize an event's first VEVENT (used for single-event lookups)."""
    return _summarize_component(event.icalendar_component)


def _search_events(cal: "caldav.Calendar", start_dt, end_dt) -> list:
    """Return all VEVENT occurrences in [start, end) as summary dicts.

    Prefers server-side recurrence expansion so recurring events appear on their
    real dates; iCloud's expand support is uneven, so this falls back to an
    unexpanded query when expansion errors. Each returned event may carry more
    than one VEVENT (expanded occurrences / overrides), so all are flattened.
    """
    events = None
    if EXPAND_RECURRENCES:
        try:
            events = cal.search(start=start_dt, end=end_dt, event=True, expand=True)
        except Exception as exc:
            logger.warning(
                "Expanded event search failed (%s: %s); retrying without expansion.",
                type(exc).__name__,
                exc,
            )
    if events is None:
        events = cal.search(start=start_dt, end=end_dt, event=True, expand=False)

    summaries = []
    for event in events:
        for vevent in event.icalendar_instance.walk("VEVENT"):
            summaries.append(_summarize_component(vevent))
    return summaries


def _scan_for_uid(cal: "caldav.Calendar", uid: str, start_dt, end_dt):
    """Return the stored event with this UID in [start, end), or None.

    `expand=False` is deliberate: an expanded occurrence is a component the
    library synthesized from an RRULE, not a resource on the server, so it has no
    href to DELETE or PUT back. The unexpanded search returns the stored objects,
    and a recurring master matches whenever any of its occurrences falls in the
    window — which is what a UID lookup wants.
    """
    for event in cal.search(start=start_dt, end=end_dt, event=True, expand=False):
        if event.id == uid:
            return event
    return None


def _find_event(cal: "caldav.Calendar", uid: str) -> "caldav.Event":
    """Resolve a UID to the stored event, with a fallback for servers that can't.

    The direct route is `event_by_uid()`, which asks the server for the object via
    a REPORT carrying a `prop-filter`/`text-match` on UID. iCloud rejects that
    query with a bare `412 Precondition Failed` — empty body, no `DAV:error`
    precondition naming what it objected to — which took out `get_event`,
    `update_event` and `delete_event` while date-range queries kept working. The
    filter python-caldav emits is well-formed and correctly nested (VCALENDAR >
    VEVENT > prop-filter name="UID" > text-match collation="i;octet"), and the
    only structural difference from the queries iCloud does answer is that a UID
    lookup carries no `time-range`. python-caldav already works around servers
    that need one, but the workaround is keyed on a quirk profile and iCloud's is
    commented out upstream, so nothing engages it here.

    So when the server-side lookup fails, fall back to what this account is known
    to serve: a time-range search, with the UID matched client-side. That covers
    the 412, and any other reason the lookup could not be answered, without this
    server having to identify which CalDAV implementation it is talking to.

    Raises caldav.error.NotFoundError when no event carries the UID.
    """
    try:
        return cal.event_by_uid(uid)
    except Exception as exc:
        # Debug, not warning: on iCloud this fires on every single lookup and the
        # fallback below then succeeds, so logging it louder would be pure noise.
        # The reason is carried into the NotFoundError instead, so it is still
        # visible in the one case where it explains an actual failure.
        logger.debug(
            "Server-side UID lookup for %r failed (%s: %s); falling back to a "
            "time-range scan.",
            uid,
            type(exc).__name__,
            exc,
        )
        lookup_error = exc

    now = datetime.now(timezone.utc)
    window = timedelta(days=UID_SCAN_WINDOW_DAYS)
    event = _scan_for_uid(cal, uid, now - window, now + window)
    if event is not None:
        return event

    # Widen before believing the event is gone: the window above is a latency
    # optimisation, not a statement about where events may live.
    try:
        event = _scan_for_uid(cal, uid, UID_SWEEP_START, UID_SWEEP_END)
    except Exception as exc:
        # A server that refuses the full range (some enforce a minimum date) must
        # not turn a lookup into an unrelated-looking error, so report it as the
        # not-found it amounts to — naming both failures, since between them they
        # say the UID could not be resolved rather than that it does not exist.
        logger.warning(
            "Widened UID sweep for %r failed (%s: %s).", uid, type(exc).__name__, exc
        )
        raise caldav.error.NotFoundError(
            f"Could not resolve UID {uid!r} in {_calendar_name(cal)!r}: the "
            f"server-side lookup failed ({type(lookup_error).__name__}: {lookup_error}) "
            f"and the fallback scan failed ({type(exc).__name__}: {exc})."
        ) from exc
    if event is not None:
        return event

    raise caldav.error.NotFoundError(
        f"No event with UID {uid!r} in {_calendar_name(cal)!r}. (The server-side "
        f"UID lookup was unavailable — {type(lookup_error).__name__}: "
        f"{lookup_error} — so the calendar was scanned instead.)"
    )


def _set_prop(vevent: IEvent, name: str, value) -> None:
    """Replace (or add) a single VEVENT property."""
    if name in vevent:
        del vevent[name]
    vevent.add(name, value)


# Tool annotations. Clients (e.g. Claude's connector settings) use these hints to
# group tools as read vs write and to decide what warrants confirmation, so every
# tool declares them. `openWorldHint` is true throughout: each call talks to an
# external CalDAV server. Named READ (not READ_ONLY) on purpose: a module-level
# `READ_ONLY = ToolAnnotations(...)` would shadow the READ_ONLY env-var boolean
# above, which _require_writable() reads at call time — permanently disabling the
# create/update/delete tools regardless of the env var.
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
# Creating adds a new event without altering existing ones, and each call makes
# another event — not destructive, not idempotent.
CREATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)
# Updating overwrites existing fields and deleting removes data: destructive, but
# repeating the same call lands on the same end state.
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
)
# Managing the subscription pull list writes server-side config, not calendar
# data. Upserts are keyed by feed URL, so re-adding the same feed is idempotent.
SUBSCRIBE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
)


# --- Read tools ---------------------------------------------------------------


@mcp.tool(annotations=READ)
def list_calendars(kind: str = "calendar") -> str:
    """List the collections available in the connected CalDAV account.

    CalDAV (including iCloud) exposes Reminders/task lists as collections
    alongside real event calendars. Subscribed ICS feeds are served here too, as
    a separate read-only source. Each entry reports its `kind` so they can be told
    apart: "calendar" (an owned, writable event calendar), "subscription" (a
    read-only ICS feed), "tasks" (a Reminders list, VTODO), or "unknown".

    Args:
        kind: Which collections to return — "calendar" (default: everything you
            can read events from, i.e. owned event calendars *and* subscriptions),
            "subscription" (only ICS feeds), "tasks" (only Reminders lists), or
            "all".

    Returns:
        A JSON array of objects with `name`, `url`, `kind`, `read_only`, and
        `components`. Subscriptions also carry `id`, `last_fetch` and
        `last_status`. ALLOWED_CALENDARS / ALLOWED_SUBSCRIPTIONS restrict what is
        returned when configured.
    """
    result = []
    if kind in ("calendar", "all"):
        for cal in _principal_calendars(_get_principal()):
            name = _calendar_name(cal)
            if ALLOWED_CALENDARS and name not in ALLOWED_CALENDARS:
                continue
            components = _supported_components(cal)
            entry_kind = _calendar_kind(components)
            # "calendar" hides only *confirmed* task lists, so a calendar whose
            # component set the server didn't advertise ("unknown") is never dropped.
            if kind == "calendar" and entry_kind == "tasks":
                continue
            result.append(
                {
                    "name": name,
                    "url": str(cal.url),
                    "kind": entry_kind,
                    "read_only": False,
                    "components": components,
                }
            )
    elif kind == "tasks":
        for cal in _principal_calendars(_get_principal()):
            name = _calendar_name(cal)
            if ALLOWED_CALENDARS and name not in ALLOWED_CALENDARS:
                continue
            components = _supported_components(cal)
            if _calendar_kind(components) != "tasks":
                continue
            result.append(
                {
                    "name": name,
                    "url": str(cal.url),
                    "kind": "tasks",
                    "read_only": False,
                    "components": components,
                }
            )

    if kind in ("calendar", "subscription", "all"):
        for entry in subscriptions.load():
            if not subscriptions.is_permitted(entry):
                continue
            result.append(
                {
                    "name": entry.get("name", ""),
                    "url": entry["url"],
                    "id": entry["id"],
                    "kind": "subscription",
                    "read_only": True,
                    "components": ["VEVENT"],
                    "last_fetch": entry.get("last_fetch"),
                    "last_status": entry.get("last_status"),
                }
            )
    return json.dumps(result)


@mcp.tool(annotations=READ)
def list_events(start: str, end: str, calendar: str | None = None) -> str:
    """List events in a calendar within a time window.

    Args:
        start: Window start as an ISO 8601 date/datetime (inclusive).
        end: Window end as an ISO 8601 date/datetime (exclusive).
        calendar: Calendar display name *or* URL, or a subscription id/URL/name.
            Falls back to DEFAULT_CALENDAR when omitted. Must resolve to a calendar
            in ALLOWED_CALENDARS when one is configured. Pass the URL or id from
            `list_calendars` to disambiguate entries that share a display name.

    Returns:
        A JSON array of events (uid, summary, start, end, location, description).
        Recurring events are expanded to one entry per occurrence in the window.
    """
    start_dt = _parse_dt(start, all_day=False)
    end_dt = _parse_dt(end, all_day=False)

    kind, resolved = _resolve_any(_resolve_target(calendar))
    if kind == "subscription":
        occurrences = subscriptions.expand_events(resolved, start_dt, end_dt)
        return json.dumps([_summarize_component(c) for c in occurrences])
    return json.dumps(_search_events(resolved, start_dt, end_dt))


@mcp.tool(annotations=READ)
def get_event(uid: str, calendar: str | None = None) -> str:
    """Fetch a single event by its UID.

    Args:
        uid: The event UID (as returned by create/list tools).
        calendar: Calendar display name/URL, or a subscription id/URL/name. Falls
            back to DEFAULT_CALENDAR.

    Returns:
        A JSON object describing the event, or a JSON `null` if not found.
    """
    kind, resolved = _resolve_any(_resolve_target(calendar))
    if kind == "subscription":
        component = subscriptions.find_event(resolved, uid)
        return json.dumps(_summarize_component(component) if component is not None else None)

    try:
        event = _find_event(resolved, uid)
    except caldav.error.NotFoundError:
        return json.dumps(None)
    return json.dumps(_summarize_event(event))


# --- Write tools --------------------------------------------------------------


@mcp.tool(annotations=CREATE)
def create_event(
    summary: str,
    start: str,
    end: str,
    calendar: str | None = None,
    description: str | None = None,
    location: str | None = None,
    all_day: bool = False,
    attendees: list[EventAttendee] | None = None,
    organizer: EventOrganizer | None = None,
    status: str | None = None,
    event_class: str | None = None,
    transparency: str | None = None,
    categories: list[str] | None = None,
    url: str | None = None,
    priority: int | None = None,
    contact: str | None = None,
    rrule: dict | None = None,
    custom_fields: dict[str, str | list[str]] | None = None,
) -> str:
    """Create a calendar event.

    Args:
        summary: The event title.
        start: Start as ISO 8601. Use `YYYY-MM-DD` for all-day events.
        end: End as ISO 8601 (exclusive). Use `YYYY-MM-DD` for all-day events.
        calendar: Calendar display name. Falls back to DEFAULT_CALENDAR. Must be
            in ALLOWED_CALENDARS when one is configured.
        description: Optional longer description / notes.
        location: Optional location string.
        all_day: When true, treat start/end as whole-day dates.
        attendees: Event participants with email, optional name, role, partstat,
            and RSVP flag. Email may be resolved from the contacts address book
            by calling `search_contacts` first.
        organizer: Optional organizer identity with email and display name.
        status, event_class, transparency, categories, url, priority, contact,
            rrule: Optional standard iCalendar VEVENT properties.
        custom_fields: X- extension properties to store in the VEVENT.

    Returns:
        A short confirmation string including the new event's UID.
    """
    _require_writable()
    cal = _resolve_writable(_resolve_target(calendar))

    uid = f"{uuid.uuid4()}@caldav-mcp"
    vevent = IEvent()
    vevent.add("uid", uid)
    vevent.add("summary", summary)
    vevent.add("dtstart", _parse_dt(start, all_day))
    vevent.add("dtend", _parse_dt(end, all_day))
    vevent.add("dtstamp", datetime.now(timezone.utc))
    if description:
        vevent.add("description", description)
    if location:
        vevent.add("location", location)
    for name, value in (
        ("status", status),
        ("class", event_class),
        ("transp", transparency),
        ("url", url),
        ("priority", priority),
        ("contact", contact),
        ("rrule", rrule),
    ):
        if value is not None:
            vevent.add(name, value)
    if categories:
        vevent.add("categories", categories)
    _set_organizer(vevent, organizer)
    _add_attendees(vevent, attendees)
    _set_custom_ical_fields(vevent, custom_fields)

    ical = ICalendar()
    ical.add("prodid", "-//caldav-mcp//EN")
    ical.add("version", "2.0")
    ical.add_component(vevent)

    cal.save_event(ical.to_ical().decode("utf-8"))
    return f"Event created in {_calendar_name(cal)!r} with UID {uid}."


@mcp.tool(annotations=DESTRUCTIVE)
def update_event(
    uid: str,
    calendar: str | None = None,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    all_day: bool = False,
    status: str | None = None,
    event_class: str | None = None,
    transparency: str | None = None,
    categories: list[str] | None = None,
    url: str | None = None,
    priority: int | None = None,
    contact: str | None = None,
    rrule: dict | None = None,
    organizer: EventOrganizer | None = None,
    custom_fields: dict[str, str | list[str] | None] | None = None,
) -> str:
    """Update fields of an existing event, identified by UID.

    Only the provided fields are changed; omitted fields are left as-is. When
    updating `start` or `end`, set `all_day` to match the event's kind.

    Args:
        uid: The UID of the event to update.
        calendar: Calendar display name. Falls back to DEFAULT_CALENDAR.
        summary: New title, if changing.
        start: New start (ISO 8601), if changing.
        end: New end (ISO 8601), if changing.
        description: New description, if changing.
        location: New location, if changing.
        all_day: Whether provided start/end are whole-day dates.
        status, event_class, transparency, categories, url, priority, contact,
            rrule, organizer: New values for the corresponding iCalendar
            properties, if changing.
        custom_fields: X- properties to add/update; use null to remove a property.

    Returns:
        A short confirmation string.
    """
    _require_writable()
    cal = _resolve_writable(_resolve_target(calendar))
    event = _find_event(cal, uid)

    ical = event.icalendar_instance
    vevent = next(c for c in ical.walk("VEVENT"))
    if summary is not None:
        _set_prop(vevent, "summary", summary)
    if start is not None:
        _set_prop(vevent, "dtstart", _parse_dt(start, all_day))
    if end is not None:
        _set_prop(vevent, "dtend", _parse_dt(end, all_day))
    if description is not None:
        _set_prop(vevent, "description", description)
    if location is not None:
        _set_prop(vevent, "location", location)
    for name, value in (
        ("status", status),
        ("class", event_class),
        ("transp", transparency),
        ("url", url),
        ("priority", priority),
        ("contact", contact),
        ("rrule", rrule),
        ("categories", categories),
    ):
        if value is not None:
            _set_ical_property(vevent, name, value)
    _set_prop(vevent, "dtstamp", datetime.now(timezone.utc))
    _set_organizer(vevent, organizer)
    _set_custom_ical_fields(vevent, custom_fields)

    event.data = ical.to_ical()
    event.save()
    return f"Event {uid} updated in {_calendar_name(cal)!r}."


def _resolve_attendee_identity(email: str | None, name: str | None) -> tuple[str, str | None]:
    if email:
        return email.strip(), name
    if not name:
        raise ValueError("Provide an attendee email or a contact name to resolve.")
    client = _get_carddav_client()
    book_url = _resolve_addressbook_url(None)
    try:
        entries = carddav.search_vcards_server_side(client, book_url, name)
        if not entries:
            entries = carddav.fetch_all_vcards(client, book_url)
    except Exception:
        entries = carddav.fetch_all_vcards(client, book_url)
    contacts = [
        carddav.vcard_to_contact(entry["text"])
        for entry in entries
    ]
    matches = [
        contact for contact in contacts
        if (contact.get("full_name") or "").casefold() == name.casefold()
    ]
    identities = {
        (entry.get("value") or "").strip()
        for contact in matches
        for entry in contact.get("emails", [])
        if entry.get("value")
    }
    if not matches or not identities:
        raise ValueError(f"No contact named {name!r} with an email address was found.")
    if len(identities) != 1:
        raise ValueError(
            f"Contact name {name!r} is ambiguous; matching contacts have different "
            "email addresses. Search contacts and specify the intended email."
        )
    return next(iter(identities)), name


def _save_attendee_change(event, ical) -> None:
    event.data = ical.to_ical()
    event.save()


@mcp.tool(annotations=DESTRUCTIVE)
def add_event_attendee(
    uid: str,
    email: str | None = None,
    name: str | None = None,
    role: AttendeeRole | None = "REQ-PARTICIPANT",
    partstat: AttendeePartstat | None = None,
    rsvp: bool | None = True,
    calendar: str | None = None,
) -> str:
    """Add a real iCalendar ATTENDEE, by email or by an exact address-book name.

    If name is given without email, it is resolved against CardDAV contacts.
    Multiple matching email addresses are reported as ambiguous instead of
    selecting one arbitrarily.
    """
    _require_writable()
    resolved_email, resolved_name = _resolve_attendee_identity(email, name)
    cal = _resolve_writable(_resolve_target(calendar))
    event = _find_event(cal, uid)
    ical = event.icalendar_instance
    vevent = next(c for c in ical.walk("VEVENT"))
    _add_attendees(vevent, [{
        "email": resolved_email,
        "name": resolved_name,
        "role": role,
        "partstat": partstat,
        "rsvp": rsvp,
    }])
    _save_attendee_change(event, ical)
    return f"Attendee {resolved_email} added to event {uid}."


@mcp.tool(annotations=DESTRUCTIVE)
def update_event_attendee(
    uid: str,
    email: str,
    name: str | None = None,
    role: AttendeeRole | None = None,
    partstat: AttendeePartstat | None = None,
    rsvp: bool | None = None,
    calendar: str | None = None,
) -> str:
    """Update CN, ROLE, PARTSTAT, and/or RSVP for an existing attendee email."""
    _require_writable()
    cal = _resolve_writable(_resolve_target(calendar))
    event = _find_event(cal, uid)
    ical = event.icalendar_instance
    vevent = next(c for c in ical.walk("VEVENT"))
    attendee = _find_attendee(vevent, email)
    if attendee is None:
        raise ValueError(f"Event {uid} has no attendee {email!r}.")
    if name is not None:
        attendee.params["CN"] = name
    if role is not None:
        value = role.upper()
        if value not in _ROLE_VALUES:
            raise ValueError(f"Invalid iCalendar attendee ROLE {value!r}.")
        attendee.params["ROLE"] = value
    if partstat is not None:
        value = partstat.upper()
        if value not in _PARTSTAT_VALUES:
            raise ValueError(f"Invalid iCalendar attendee PARTSTAT {value!r}.")
        attendee.params["PARTSTAT"] = value
    if rsvp is not None:
        attendee.params["RSVP"] = "TRUE" if rsvp else "FALSE"
    _save_attendee_change(event, ical)
    return f"Attendee {email} updated on event {uid}."


@mcp.tool(annotations=DESTRUCTIVE)
def remove_event_attendee(uid: str, email: str, calendar: str | None = None) -> str:
    """Remove one ATTENDEE by calendar email identity without changing others."""
    _require_writable()
    cal = _resolve_writable(_resolve_target(calendar))
    event = _find_event(cal, uid)
    ical = event.icalendar_instance
    vevent = next(c for c in ical.walk("VEVENT"))
    attendee = _find_attendee(vevent, email)
    if attendee is None:
        raise ValueError(f"Event {uid} has no attendee {email!r}.")
    remaining = [a for a in _ical_values(vevent, "attendee") if a is not attendee]
    del vevent["ATTENDEE"]
    for other in remaining:
        vevent.add("attendee", other)
    event.data = ical.to_ical()
    event.save()
    return f"Attendee {email} removed from event {uid}."


@mcp.tool(annotations=READ)
def list_event_attendees(uid: str, calendar: str | None = None) -> str:
    """List event attendees; enrich matching emails with contact names and UIDs."""
    cal = _resolve_calendar(calendar)
    event = _find_event(cal, uid)
    ical = event.icalendar_instance
    vevent = next(c for c in ical.walk("VEVENT"))
    attendees = [_attendee_dict(a) for a in _ical_values(vevent, "attendee")]
    try:
        client = _get_carddav_client()
        book_url = _resolve_addressbook_url(None)
        entries = carddav.fetch_all_vcards(client, book_url)
        by_email = {}
        for entry in entries:
            contact = carddav.vcard_to_contact(entry["text"])
            for address in contact.get("emails", []):
                if address.get("value"):
                    by_email[address["value"].casefold()] = contact
        for attendee in attendees:
            contact = by_email.get(attendee["email"].casefold())
            if contact:
                attendee["contact_uid"] = contact.get("uid")
                attendee["contact_name"] = contact.get("full_name")
    except Exception as exc:
        logger.debug("Could not resolve event attendees against contacts: %s", exc)
    return json.dumps(attendees)


@mcp.tool(annotations=DESTRUCTIVE)
def delete_event(uid: str, calendar: str | None = None) -> str:
    """Delete an event by its UID.

    Args:
        uid: The UID of the event to delete.
        calendar: Calendar display name. Falls back to DEFAULT_CALENDAR. Must be
            in ALLOWED_CALENDARS when one is configured.

    Returns:
        A short confirmation string.
    """
    _require_writable()
    cal = _resolve_writable(_resolve_target(calendar))
    _find_event(cal, uid).delete()
    return f"Event {uid} deleted from {_calendar_name(cal)!r}."


# --- Subscription tools --------------------------------------------------------


@mcp.tool(annotations=SUBSCRIBE)
def add_subscription(name: str, url: str) -> str:
    """Subscribe to a read-only ICS calendar feed and serve it alongside the calendars.

    Use this for calendars that CalDAV cannot reach — notably Apple "subscribed
    calendars" (team/league schedules, holiday feeds), which iCloud keeps
    device-side and never exposes over CalDAV. Once added, the feed's events are
    readable via `list_events`/`get_event` like any other calendar. Feeds are
    always read-only.

    The feed is fetched once here to validate it, so a bad URL fails now rather
    than silently returning nothing later. Adding the same URL twice just
    refreshes its name.

    Args:
        name: Display name for the feed, e.g. "Caleb Soccer".
        url: The feed URL. `webcal://` links (what Apple hands out) are accepted
            and rewritten to `https://`.

    Returns:
        A short confirmation naming the assigned id and how many events the feed
        reports over the next 90 days.
    """
    _require_writable()
    normalized = subscriptions.normalize_url(url)

    # Validate before persisting, and report what the feed actually holds. Size
    # and VEVENT count are reported alongside the occurrence count because a feed
    # can be valid iCalendar with no events yet — without the size, that is
    # indistinguishable from a broken URL.
    report = subscriptions.probe(normalized, days=90)

    action = subscriptions.upsert(name, normalized)
    # The validating fetch above ran before the entry existed, so stamp it now.
    subscriptions.record_status(
        normalized, f"ok (validated on add, {report['bytes']} bytes)"
    )
    added = subscriptions.resolve(normalized)

    summary = (
        f"Subscription {action} — {name!r} (id {added['id']}): "
        f"{report['bytes']} bytes, {report['vevents']} VEVENT(s), "
        f"{report['occurrences']} event(s) in the next 90 days."
    )
    if not report["vevents"]:
        summary += (
            " The feed is valid iCalendar but publishes no events yet — it will "
            "start returning them automatically once the publisher adds some."
        )
    return summary


@mcp.tool(annotations=READ)
def list_subscriptions() -> str:
    """List the subscribed ICS feeds and their last fetch result.

    Returns:
        A JSON array of objects with `id`, `name`, `url`, `read_only`, `added_at`,
        `last_fetch`, and `last_status`. When ALLOWED_SUBSCRIPTIONS is configured,
        only permitted feeds are returned.
    """
    return json.dumps([e for e in subscriptions.load() if subscriptions.is_permitted(e)])


@mcp.tool(annotations=DESTRUCTIVE)
def remove_subscription(id_or_url: str) -> str:
    """Remove a subscribed ICS feed from the pull list.

    This only stops serving the feed here; it does not touch the feed itself or
    any iCloud calendar.

    Args:
        id_or_url: The subscription's id (from `list_subscriptions`), its URL,
            or its display name.

    Returns:
        A short confirmation, or a note that nothing matched.
    """
    _require_writable()
    removed = subscriptions.remove(id_or_url)
    if removed is None:
        return f"No subscription matched {id_or_url!r}; nothing removed."
    return f"Removed subscription {removed.get('name') or ''!r} (id {removed['id']})."


# --- Contacts / CardDAV tools --------------------------------------------------


@mcp.tool(annotations=READ)
def list_contact_books() -> str:
    """List the CardDAV address books accessible to the configured contacts owner.

    The owner is `CARDDAV_CONTACTS_USER` (falling back to `CALDAV_USERNAME` when
    unset) — independent of `CALDAV_CALENDAR_USER`. Authentication still always
    uses `CALDAV_USERNAME`/`CALDAV_PASSWORD`.

    Returns:
        A JSON array of objects with `name`, `url`, `permissions` (e.g.
        `["read", "write"]`), and `components` (`["VCARD"]`).
    """
    client = _get_carddav_client()
    books = carddav.discover_addressbooks(client, _egroupware_addressbook_home_url())
    if not books:
        # Some EGroupware configurations don't expose the home-set listing to
        # every account; fall back to probing the owner's default address book
        # directly, the same resilience _principal_calendars() relies on.
        url = _egroupware_addressbook_url()
        carddav.list_member_hrefs(client, url)  # raises if the book is unreachable
        books = [{"name": "addressbook", "url": url, "permissions": ["read", "write"], "components": ["VCARD"]}]
    return json.dumps(books)


@mcp.tool(annotations=READ)
def list_contacts(address_book: str | None = None, limit: int | None = None, offset: int = 0) -> str:
    """List contacts in an address book.

    Args:
        address_book: Address book display name or URL (from
            `list_contact_books`). Defaults to the configured contacts owner's
            default address book when omitted.
        limit: Maximum number of contacts to return.
        offset: Number of contacts to skip (for pagination). No particular
            sort order is guaranteed — the server's own listing order is used.

    Returns:
        A JSON array of structured contact objects (see `get_contact` for the
        field list).
    """
    client = _get_carddav_client()
    url = _resolve_addressbook_url(address_book)
    entries = carddav.fetch_all_vcards(client, url)
    contacts = [
        carddav.vcard_to_contact(e["text"], href=e["href"], etag=e["etag"]) for e in entries
    ]
    contacts = contacts[offset:]
    if limit is not None:
        contacts = contacts[:limit]
    return json.dumps(contacts)


@mcp.tool(annotations=READ)
def get_contact(uid: str, address_book: str | None = None) -> str:
    """Fetch a single contact by UID.

    Args:
        uid: The contact's UID (as returned by `list_contacts`/`search_contacts`/
            `create_contact`).
        address_book: Address book display name or URL. Defaults to the
            configured contacts owner's default address book.

    Returns:
        A JSON object with the contact's structured fields, or JSON `null` if
        not found.
    """
    client = _get_carddav_client()
    url = _resolve_addressbook_url(address_book)
    try:
        entry = _find_contact(client, url, uid)
    except caldav.error.NotFoundError:
        return json.dumps(None)
    return json.dumps(carddav.vcard_to_contact(entry["text"], href=entry["href"], etag=entry["etag"]))


@mcp.tool(annotations=READ)
def search_contacts(
    query: str,
    address_book: str | None = None,
    limit: int | None = None,
    category: str | None = None,
    categories: list[str] | None = None,
) -> str:
    """Search contacts by name, organization, email, phone, or category.

    Tries a server-side CardDAV search first (one request); if the server
    rejects or does not support it, falls back to fetching the address book
    once and matching client-side — never more than one round trip either way.

    Args:
        query: Substring to search for (case-insensitive) across full name,
            given/family name, organization, emails, phones, and categories.
        address_book: Address book display name or URL. Defaults to the
            configured contacts owner's default address book.
        limit: Maximum number of results to return.
        category: Require this exact category (case-insensitive).
        categories: Require every listed category (case-insensitive).

    Returns:
        A JSON array of structured contact objects matching the query.
    """
    client = _get_carddav_client()
    url = _resolve_addressbook_url(address_book)
    try:
        entries = carddav.search_vcards_server_side(client, url, query)
        contacts = [
            carddav.vcard_to_contact(e["text"], href=e["href"], etag=e["etag"]) for e in entries
        ]
    except Exception as exc:
        logger.debug(
            "Server-side contact search failed (%s: %s); scanning the address "
            "book instead.",
            type(exc).__name__,
            exc,
        )
        entries = carddav.fetch_all_vcards(client, url)
        contacts = [
            carddav.vcard_to_contact(e["text"], href=e["href"], etag=e["etag"]) for e in entries
        ]
        contacts = [c for c in contacts if carddav.matches_query(c, query)]
    required_categories = {c.casefold() for c in (categories or [])}
    if category:
        required_categories.add(category.casefold())
    if required_categories:
        contacts = [
            contact for contact in contacts
            if required_categories.issubset(
                {value.casefold() for value in contact.get("categories", [])}
            )
        ]
    if limit is not None:
        contacts = contacts[:limit]
    return json.dumps(contacts)


@mcp.tool(annotations=CREATE)
def create_contact(
    full_name: str | None = None,
    given_name: str | None = None,
    family_name: str | None = None,
    organization: str | None = None,
    title: str | None = None,
    emails: list[ContactValue] | None = None,
    phones: list[ContactValue] | None = None,
    addresses: list[ContactAddress] | None = None,
    birthday: str | None = None,
    notes: str | None = None,
    url: str | None = None,
    categories: list[str] | None = None,
    address_book: str | None = None,
    additional_name: str | None = None,
    name_prefix: str | None = None,
    name_suffix: str | None = None,
    nickname: str | None = None,
    role: str | None = None,
    kind: str | None = None,
    anniversary: str | None = None,
    impp: list[ContactValue] | None = None,
    photos: list[ContactValue] | None = None,
    custom_fields: dict[str, str | list[str]] | None = None,
) -> str:
    """Create a contact.

    At least one of `full_name`, `given_name`, or `family_name` is required.

    Args:
        full_name: Display name (vCard `FN`). Synthesized from given/family
            name when omitted.
        given_name: First name.
        family_name: Last name / surname.
        organization: Company/organization name.
        title: Job title.
        emails: List of `{"value": "...", "type": "HOME"|"WORK"|...}`.
        phones: List of `{"value": "...", "type": "CELL"|"HOME"|"WORK"|...}`.
        addresses: List of `{"type": ..., "street", "city", "region", "code",
            "country"}` (any may be omitted).
        birthday: `YYYY-MM-DD`, or `--MM-DD` if the year is unknown.
        notes: Free-text notes.
        url: A homepage/profile URL.
        categories: List of category tags.
        additional_name, name_prefix, name_suffix: Additional vCard N components.
        nickname, role, kind, anniversary, impp, photos: Additional vCard fields.
        custom_fields: User-defined X- or otherwise unmodeled properties.
        address_book: Address book display name or URL. Defaults to the
            configured contacts owner's default address book.

    Returns:
        A short confirmation string including the new contact's UID.
    """
    _require_contact_writable()
    client = _get_carddav_client()
    book_url = _resolve_addressbook_url(address_book)

    uid = f"{uuid.uuid4()}"
    vcard_text = carddav.build_vcard(
        {
            "uid": uid,
            "full_name": full_name,
            "given_name": given_name,
            "family_name": family_name,
            "organization": organization,
            "title": title,
            "emails": emails,
            "phones": phones,
            "addresses": addresses,
            "birthday": birthday,
            "notes": notes,
            "url": url,
            "categories": categories,
            "name_components": {
                "additional": additional_name,
                "prefix": name_prefix,
                "suffix": name_suffix,
            },
            "nickname": nickname,
            "role": role,
            "kind": kind,
            "anniversary": anniversary,
            "impp": impp,
            "photos": photos,
            "custom_fields": custom_fields,
        }
    )
    href = f"{book_url.rstrip('/')}/{uid}.vcf"
    carddav.put_vcard(client, href, vcard_text)
    return f"Contact created with UID {uid}."


@mcp.tool(annotations=DESTRUCTIVE)
def update_contact(
    uid: str,
    address_book: str | None = None,
    full_name: str | None = None,
    given_name: str | None = None,
    family_name: str | None = None,
    organization: str | None = None,
    title: str | None = None,
    emails: list[ContactValue] | None = None,
    phones: list[ContactValue] | None = None,
    addresses: list[ContactAddress] | None = None,
    birthday: str | None = None,
    notes: str | None = None,
    url: str | None = None,
    categories: list[str] | None = None,
    additional_name: str | None = None,
    name_prefix: str | None = None,
    name_suffix: str | None = None,
    nickname: str | None = None,
    role: str | None = None,
    kind: str | None = None,
    anniversary: str | None = None,
    impp: list[ContactValue] | None = None,
    photos: list[ContactValue] | None = None,
    custom_fields: dict[str, str | list[str] | None] | None = None,
) -> str:
    """Update fields of an existing contact, identified by UID.

    Read-modify-write: only the fields explicitly passed are changed, and
    scalar fields left as `None` (title, organization, birthday, notes, url,
    full/given/family name) are preserved untouched. List fields (emails,
    phones, addresses, categories, IM handles, photos) are replaced *in full*
    when provided. Every omitted property is preserved from the stored vCard.

    Args:
        uid: The UID of the contact to update.
        address_book: Address book display name or URL. Defaults to the
            configured contacts owner's default address book.
        All fields are optional. List fields replace their complete values;
            `custom_fields` updates X- or otherwise unmodeled properties; null
            removes the named property.

    Returns:
        A short confirmation string.
    """
    _require_contact_writable()
    client = _get_carddav_client()
    book_url = _resolve_addressbook_url(address_book)
    entry = _find_contact(client, book_url, uid)

    new_text = carddav.apply_updates(
        entry["text"],
        {
            "full_name": full_name,
            "given_name": given_name,
            "family_name": family_name,
            "organization": organization,
            "title": title,
            "emails": emails,
            "phones": phones,
            "addresses": addresses,
            "birthday": birthday,
            "notes": notes,
            "url": url,
            "categories": categories,
            "name_components": {
                "additional": additional_name,
                "prefix": name_prefix,
                "suffix": name_suffix,
            } if any(v is not None for v in (additional_name, name_prefix, name_suffix)) else None,
            "nickname": nickname,
            "role": role,
            "kind": kind,
            "anniversary": anniversary,
            "impp": impp,
            "photos": photos,
            "custom_fields": custom_fields,
        },
    )
    carddav.put_vcard(client, entry["href"], new_text, etag=entry["etag"])
    return f"Contact {uid} updated."


def _write_contact_categories(uid: str, categories: list[str], address_book: str | None) -> None:
    _require_contact_writable()
    client = _get_carddav_client()
    book_url = _resolve_addressbook_url(address_book)
    entry = _find_contact(client, book_url, uid)
    text = carddav.apply_updates(entry["text"], {"categories": categories})
    carddav.put_vcard(client, entry["href"], text, etag=entry["etag"])


@mcp.tool(annotations=READ)
def get_contact_categories(uid: str, address_book: str | None = None) -> str:
    """Return a contact's CATEGORIES values."""
    client = _get_carddav_client()
    book_url = _resolve_addressbook_url(address_book)
    entry = _find_contact(client, book_url, uid)
    return json.dumps(carddav.vcard_to_contact(entry["text"]).get("categories", []))


@mcp.tool(annotations=DESTRUCTIVE)
def set_contact_categories(
    uid: str, categories: list[str], address_book: str | None = None
) -> str:
    """Replace the complete CATEGORIES list; pass an empty list to clear it."""
    _write_contact_categories(uid, categories, address_book)
    return f"Categories for contact {uid} replaced."


@mcp.tool(annotations=DESTRUCTIVE)
def add_contact_category(uid: str, category: str, address_book: str | None = None) -> str:
    """Add a category if it is not already present, ignoring case for duplicates."""
    _require_contact_writable()
    client = _get_carddav_client()
    book_url = _resolve_addressbook_url(address_book)
    entry = _find_contact(client, book_url, uid)
    existing = carddav.vcard_to_contact(entry["text"]).get("categories", [])
    if not any(value.casefold() == category.casefold() for value in existing):
        existing.append(category)
    text = carddav.apply_updates(entry["text"], {"categories": existing})
    carddav.put_vcard(client, entry["href"], text, etag=entry["etag"])
    return f"Category {category!r} added to contact {uid}."


@mcp.tool(annotations=DESTRUCTIVE)
def remove_contact_category(uid: str, category: str, address_book: str | None = None) -> str:
    """Remove a category by case-insensitive name without changing other categories."""
    _require_contact_writable()
    client = _get_carddav_client()
    book_url = _resolve_addressbook_url(address_book)
    entry = _find_contact(client, book_url, uid)
    existing = carddav.vcard_to_contact(entry["text"]).get("categories", [])
    updated = [value for value in existing if value.casefold() != category.casefold()]
    text = carddav.apply_updates(entry["text"], {"categories": updated})
    carddav.put_vcard(client, entry["href"], text, etag=entry["etag"])
    return f"Category {category!r} removed from contact {uid}."


@mcp.tool(annotations=DESTRUCTIVE)
def delete_contact(uid: str, address_book: str | None = None) -> str:
    """Delete a contact by UID.

    Identifies the contact strictly by UID — never by name — so two contacts
    sharing a display name are never confused. If you only have a name, call
    `search_contacts` first and pass back the UID of the one to delete.

    Args:
        uid: The UID of the contact to delete.
        address_book: Address book display name or URL. Defaults to the
            configured contacts owner's default address book.

    Returns:
        A short confirmation string.
    """
    _require_contact_writable()
    client = _get_carddav_client()
    book_url = _resolve_addressbook_url(address_book)
    entry = _find_contact(client, book_url, uid)
    carddav.delete_vcard(client, entry["href"], etag=entry["etag"])
    return f"Contact {uid} deleted."


@mcp.tool(annotations=READ)
def list_birthdays(
    start_date: str | None = None, end_date: str | None = None, address_book: str | None = None
) -> str:
    """List upcoming birthdays, sourced from contacts' `BDAY` field.

    Deliberately independent of any EGroupware-generated calendar birthday
    events: this reads vCard `BDAY` directly, so it works even when the
    calendar owner (`CALDAV_CALENDAR_USER`) and contacts owner
    (`CARDDAV_CONTACTS_USER`) differ or when no birthday calendar exists.

    Args:
        start_date: Window start as `YYYY-MM-DD` (inclusive). Defaults to today.
        end_date: Window end as `YYYY-MM-DD` (inclusive). Defaults to 30 days
            after `start_date`.
        address_book: Address book display name or URL. Defaults to the
            configured contacts owner's default address book.

    Returns:
        A JSON array of objects with `name`, `birthday` (the next occurrence,
        `YYYY-MM-DD`), `age` (the age they will turn, or `null` when the birth
        year is unknown), and `uid`, sorted by date. Recurs every year by
        construction: each entry is the *next* occurrence of that month/day on
        or after `start_date`.
    """
    start = date.fromisoformat(start_date[:10]) if start_date else date.today()
    end = date.fromisoformat(end_date[:10]) if end_date else start + timedelta(days=30)

    client = _get_carddav_client()
    book_url = _resolve_addressbook_url(address_book)
    entries = carddav.fetch_all_vcards(client, book_url)

    results = []
    for entry in entries:
        contact = carddav.vcard_to_contact(entry["text"])
        raw_bday = contact.get("birthday")
        if not raw_bday:
            continue
        try:
            month, day, year = carddav.parse_birthday(raw_bday)
        except ValueError:
            logger.debug("Unparseable BDAY value for contact %r.", contact.get("uid"))
            continue
        occurrence = carddav.next_occurrence(month, day, start)
        if occurrence > end:
            continue
        age = occurrence.year - year if year is not None else None
        name = contact.get("full_name") or " ".join(
            p for p in (contact.get("given_name"), contact.get("family_name")) if p
        )
        results.append(
            {
                "name": name or None,
                "birthday": occurrence.isoformat(),
                "age": age,
                "uid": contact.get("uid"),
            }
        )
    results.sort(key=lambda r: r["birthday"])
    return json.dumps(results)


def _run_startup_test() -> None:
    """Connect to CalDAV at startup to verify config. Never raises.

    Lists the account's calendars (a cheap authenticated round-trip). On failure
    the reason is logged (auth / connection / discovery) and the server starts
    anyway so a transient CalDAV outage does not block boot.
    """
    logger.info("STARTUP_TEST enabled — connecting to CalDAV to verify config...")
    try:
        calendars = _get_principal().calendars()
    except Exception as exc:
        logger.error(
            "Startup CalDAV check FAILED against %s as %s — %s: %s. "
            "The server will keep running; fix the CalDAV settings and restart to retest.",
            CALDAV_URL,
            CALDAV_USERNAME or "<unset>",
            type(exc).__name__,
            exc,
        )
        return
    names = [_calendar_name(c) or "<unnamed>" for c in calendars]
    logger.info("Startup CalDAV check OK — %d calendar(s): %s", len(names), ", ".join(names))


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> PlainTextResponse:
    """Unauthenticated liveness probe used by Docker/compose healthchecks."""
    return PlainTextResponse("ok")


_jwks_client = None  # lazily constructed jwt.PyJWKClient (caches signing keys)


def _get_jwks_client():
    global _jwks_client
    if _jwks_client is None:
        import jwt  # PyJWT

        _jwks_client = jwt.PyJWKClient(POMERIUM_JWKS_URL)
    return _jwks_client


def _extract_assertion(headers) -> str | None:
    """Return the first present Pomerium assertion header value, else None."""
    for name in POMERIUM_ASSERTION_HEADERS:
        value = headers.get(name)
        if value:
            return value
    return None


def _verify_assertion(token: str) -> None:
    """Verify Pomerium's assertion JWT: signature (ES256) + exp + optional aud/iss.

    Raises on any failure (bad/expired/forged token). Runs sync network I/O to the
    JWKS endpoint on first use, then serves cached keys.
    """
    import jwt  # PyJWT

    signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
    jwt.decode(
        token,
        signing_key.key,
        algorithms=["ES256"],
        audience=POMERIUM_AUDIENCE or None,
        issuer=POMERIUM_ISSUER or None,
        options={
            "require": ["exp"],
            "verify_aud": bool(POMERIUM_AUDIENCE),
            "verify_iss": bool(POMERIUM_ISSUER),
        },
    )


def _transport_security() -> TransportSecuritySettings:
    """Build the SDK's Host/Origin allowlist for this deployment.

    MCP SDK 2.x turns DNS-rebinding protection on by default and, when handed a
    loopback bind address, allows only localhost. This server binds 0.0.0.0 and is
    reached through Pomerium, so requests arrive carrying the public route host —
    which that default rejects with 421 while `/healthz` keeps returning 200, i.e.
    the container looks healthy while every tool call fails.

    Set MCP_ALLOWED_HOSTS to the route host to keep the guard on (recommended).
    Left empty, the guard is switched off and the fronting proxy is relied on as
    the only Host-header check — the pre-2.x behaviour, kept as the default so an
    SDK upgrade alone cannot take a working deployment offline.
    """
    if not MCP_ALLOWED_HOSTS:
        logger.warning(
            "MCP_ALLOWED_HOSTS is not set — the DNS-rebinding guard is disabled and "
            "any Host header is accepted. Set it to the Pomerium route host "
            "(e.g. caldav-mcp.example.com) to enable it."
        )
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    origins = MCP_ALLOWED_ORIGINS or [f"https://{h}" for h in MCP_ALLOWED_HOSTS]
    logger.info("DNS-rebinding guard enabled — allowed hosts: %s", ", ".join(MCP_ALLOWED_HOSTS))
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=MCP_ALLOWED_HOSTS,
        allowed_origins=origins,
    )


def _run_with_identity_gate() -> None:
    """Serve the MCP app, cryptographically verifying Pomerium's identity on /mcp.

    Defense-in-depth: the external proxy remains the primary gate. Every /mcp
    request must carry a Pomerium assertion whose JWT verifies against Pomerium's
    JWKS; otherwise it is rejected with 401. `/healthz` stays open for healthchecks.
    """
    import uvicorn
    from starlette.concurrency import run_in_threadpool
    from starlette.middleware.base import BaseHTTPMiddleware

    app = mcp.streamable_http_app(host=HOST, transport_security=_transport_security())

    async def require_identity(request: Request, call_next):
        if request.url.path.startswith("/mcp"):
            token = _extract_assertion(request.headers)
            if not token:
                logger.warning("Rejected /mcp request: missing Pomerium assertion header.")
                return PlainTextResponse(
                    "Missing authorization proxy identity header.", status_code=401
                )
            try:
                await run_in_threadpool(_verify_assertion, token)
            except Exception as exc:
                # Log the reason (expired / bad signature / wrong audience), never the token.
                logger.warning(
                    "Rejected /mcp request: invalid Pomerium assertion — %s: %s",
                    type(exc).__name__,
                    exc,
                )
                return PlainTextResponse(
                    "Invalid authorization proxy identity.", status_code=401
                )
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=require_identity)
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Merge any env-declared ICS feeds into the persisted pull list. Never raises
    # and never fetches, so a bad or slow feed cannot block startup.
    subscriptions.seed_from_env()

    if STARTUP_TEST:
        _run_startup_test()

    if REQUIRE_POMERIUM_IDENTITY:
        if not POMERIUM_JWKS_URL:
            logger.error(
                "REQUIRE_POMERIUM_IDENTITY=true but POMERIUM_JWKS_URL is not set. "
                "The gate cannot verify assertions; refusing to start. Set POMERIUM_JWKS_URL "
                "(e.g. https://<route-host>/.well-known/pomerium/jwks.json) and "
                "POMERIUM_AUDIENCE, or set REQUIRE_POMERIUM_IDENTITY=false."
            )
            raise SystemExit(1)
        _quiet_healthz_logging()
        _run_with_identity_gate()
    else:
        _quiet_healthz_logging()
        # host/port moved off the constructor in SDK 2.x; omitting them here would
        # silently bind 127.0.0.1:8000 and leave the container unreachable.
        mcp.run(
            transport="streamable-http",
            host=HOST,
            port=PORT,
            transport_security=_transport_security(),
        )
