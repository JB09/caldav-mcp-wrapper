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
import uuid
from datetime import date, datetime, timedelta, timezone

import caldav
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent

import subscriptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import PlainTextResponse

logger = logging.getLogger("caldav-mcp")

# --- Configuration (all from env; secrets injected at runtime, never baked in) ---
# iCloud's CalDAV entry point. The client performs principal/calendar discovery
# from here, following redirects to the account's partition host.
CALDAV_URL = os.environ.get("CALDAV_URL", "https://caldav.icloud.com/")
# For iCloud this is your Apple ID (full email address).
CALDAV_USERNAME = os.environ.get("CALDAV_USERNAME", "")
# For iCloud this is an app-specific password
# (Apple ID -> Sign-In and Security -> App-Specific Passwords), NOT your login.
CALDAV_PASSWORD = os.environ.get("CALDAV_PASSWORD", "")
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

mcp = MCPServer("caldav-mcp")


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


def _resolve_calendar(name: str | None) -> "caldav.Calendar":
    """Resolve a calendar by display name *or* URL, enforcing the allowlist.

    `target` may be a calendar URL (as returned by `list_calendars`) — this
    disambiguates accounts with duplicate display names (e.g. two "Family"
    calendars). Otherwise it is matched by display name. The allowlist is checked
    against the resolved calendar's name. Raises ValueError when no calendar is
    selected/found or it is not permitted — all *before* any mutating call.
    """
    target = (name or DEFAULT_CALENDAR).strip()
    if not target:
        raise ValueError("No calendar: pass `calendar` or set DEFAULT_CALENDAR.")

    calendars = _get_principal().calendars()
    match = None
    if target.startswith(("http://", "https://")):
        for cal in calendars:
            if str(cal.url).rstrip("/") == target.rstrip("/"):
                match = cal
                break
    if match is None:
        for cal in calendars:
            if _calendar_name(cal) == target:
                match = cal
                break
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
    """Extract the interesting fields of a single VEVENT into a plain dict."""
    return {
        "uid": str(comp.get("uid", "")),
        "summary": str(comp.get("summary", "")),
        "start": _isoformat(comp.get("dtstart")),
        "end": _isoformat(comp.get("dtend")),
        "location": str(comp["location"]) if "location" in comp else None,
        "description": str(comp["description"]) if "description" in comp else None,
    }


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
        for cal in _get_principal().calendars():
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
        for cal in _get_principal().calendars():
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
        event = resolved.event_by_uid(uid)
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

    Returns:
        A short confirmation string.
    """
    _require_writable()
    cal = _resolve_writable(_resolve_target(calendar))
    event = cal.event_by_uid(uid)

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
    _set_prop(vevent, "dtstamp", datetime.now(timezone.utc))

    event.data = ical.to_ical()
    event.save()
    return f"Event {uid} updated in {_calendar_name(cal)!r}."


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
    cal.event_by_uid(uid).delete()
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
