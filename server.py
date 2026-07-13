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
from datetime import date, datetime, timezone

import caldav
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent
from mcp.server.fastmcp import FastMCP
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

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

mcp = FastMCP("caldav-mcp", host=HOST, port=PORT)

_principal = None  # lazily constructed caldav.Principal (caches the DAV session)


def _get_principal() -> "caldav.Principal":
    """Return a cached CalDAV principal, opening the session on first use.

    Raises RuntimeError if credentials are missing (before any network I/O).
    """
    global _principal
    if _principal is None:
        if not (CALDAV_USERNAME and CALDAV_PASSWORD):
            raise RuntimeError(
                "CALDAV_USERNAME and CALDAV_PASSWORD must be configured to reach CalDAV."
            )
        client = caldav.DAVClient(
            url=CALDAV_URL, username=CALDAV_USERNAME, password=CALDAV_PASSWORD
        )
        _principal = client.principal()
    return _principal


def _calendar_name(cal: "caldav.Calendar") -> str:
    """Return a calendar's display name across caldav versions.

    caldav 3.x deprecated the `.name` attribute in favour of
    `get_display_name()`; fall back to `.name` on older releases.
    """
    getter = getattr(cal, "get_display_name", None)
    if getter is not None:
        return getter() or ""
    return cal.name or ""


def _resolve_calendar(name: str | None) -> "caldav.Calendar":
    """Resolve a calendar by display name, enforcing the allowlist and default.

    Raises ValueError when no calendar is selected/found or the name is not
    permitted; everything here happens *before* the mutating network call.
    """
    target = (name or DEFAULT_CALENDAR).strip()
    if not target:
        raise ValueError("No calendar: pass `calendar` or set DEFAULT_CALENDAR.")

    # Calendar hard-limit: even a misused tool cannot touch calendars off the list.
    if ALLOWED_CALENDARS and target not in ALLOWED_CALENDARS:
        raise ValueError(
            f"Calendar {target!r} is not permitted. "
            f"Allowed calendars: {', '.join(ALLOWED_CALENDARS)}."
        )

    for cal in _get_principal().calendars():
        if _calendar_name(cal) == target:
            return cal
    raise ValueError(f"Calendar {target!r} was not found in the account.")


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


def _summarize_event(event: "caldav.Event") -> dict:
    """Extract the interesting fields of an event's VEVENT into a plain dict."""
    comp = event.icalendar_component
    return {
        "uid": str(comp.get("uid", "")),
        "summary": str(comp.get("summary", "")),
        "start": _isoformat(comp.get("dtstart")),
        "end": _isoformat(comp.get("dtend")),
        "location": str(comp["location"]) if "location" in comp else None,
        "description": str(comp["description"]) if "description" in comp else None,
    }


def _set_prop(vevent: IEvent, name: str, value) -> None:
    """Replace (or add) a single VEVENT property."""
    if name in vevent:
        del vevent[name]
    vevent.add(name, value)


# --- Read tools ---------------------------------------------------------------


@mcp.tool()
def list_calendars() -> str:
    """List the calendars available in the connected CalDAV account.

    Returns:
        A JSON array of objects with `name` and `url` for each calendar. When
        ALLOWED_CALENDARS is configured, only permitted calendars are returned.
    """
    result = []
    for cal in _get_principal().calendars():
        name = _calendar_name(cal)
        if ALLOWED_CALENDARS and name not in ALLOWED_CALENDARS:
            continue
        result.append({"name": name, "url": str(cal.url)})
    return json.dumps(result)


@mcp.tool()
def list_events(start: str, end: str, calendar: str | None = None) -> str:
    """List events in a calendar within a time window.

    Args:
        start: Window start as an ISO 8601 date/datetime (inclusive).
        end: Window end as an ISO 8601 date/datetime (exclusive).
        calendar: Calendar display name. Falls back to DEFAULT_CALENDAR when
            omitted. Must be in ALLOWED_CALENDARS when one is configured.

    Returns:
        A JSON array of events (uid, summary, start, end, location, description).
    """
    cal = _resolve_calendar(calendar)
    events = cal.search(
        start=_parse_dt(start, all_day=False),
        end=_parse_dt(end, all_day=False),
        event=True,
        expand=True,
    )
    return json.dumps([_summarize_event(e) for e in events])


@mcp.tool()
def get_event(uid: str, calendar: str | None = None) -> str:
    """Fetch a single event by its UID.

    Args:
        uid: The event UID (as returned by create/list tools).
        calendar: Calendar display name. Falls back to DEFAULT_CALENDAR.

    Returns:
        A JSON object describing the event, or a JSON `null` if not found.
    """
    cal = _resolve_calendar(calendar)
    try:
        event = cal.event_by_uid(uid)
    except caldav.error.NotFoundError:
        return json.dumps(None)
    return json.dumps(_summarize_event(event))


# --- Write tools --------------------------------------------------------------


@mcp.tool()
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
    cal = _resolve_calendar(calendar)

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


@mcp.tool()
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
    cal = _resolve_calendar(calendar)
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


@mcp.tool()
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
    cal = _resolve_calendar(calendar)
    cal.event_by_uid(uid).delete()
    return f"Event {uid} deleted from {_calendar_name(cal)!r}."


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


def _run_with_identity_gate() -> None:
    """Serve the MCP app, cryptographically verifying Pomerium's identity on /mcp.

    Defense-in-depth: the external proxy remains the primary gate. Every /mcp
    request must carry a Pomerium assertion whose JWT verifies against Pomerium's
    JWKS; otherwise it is rejected with 401. `/healthz` stays open for healthchecks.
    """
    import uvicorn
    from starlette.concurrency import run_in_threadpool
    from starlette.middleware.base import BaseHTTPMiddleware

    app = mcp.streamable_http_app()

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
        _run_with_identity_gate()
    else:
        mcp.run(transport="streamable-http")
