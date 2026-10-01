# caldav-mcp-wrapper

A minimal, self-hosted [MCP](https://modelcontextprotocol.io/) server that exposes
read and write tools for a CalDAV calendar. It is designed for Apple iCloud and
works with other CalDAV servers, including EGroupware.

## Tools

| Tool | Purpose |
| --- | --- |
| `list_calendars` | List available calendars and subscribed ICS feeds. |
| `list_events` | List events within a start/end window. |
| `get_event` | Fetch an event by UID. |
| `create_event` | Create a timed or all-day event. |
| `update_event` | Update an event by UID. |
| `delete_event` | Delete an event by UID. |
| `list_subscriptions` | List subscribed ICS feeds and their last fetch result. |
| `add_subscription` | Subscribe to an ICS feed URL. |
| `remove_subscription` | Stop serving a feed by id, URL, or name. |
| `list_contact_books` | List address books accessible to the configured contacts owner. |
| `list_contacts` | List contacts in an address book. |
| `get_contact` | Fetch a contact by UID. |
| `search_contacts` | Search contacts by name, organization, email, phone, or category. |
| `create_contact` | Create a contact. |
| `update_contact` | Update a contact by UID (read-modify-write; omitted fields are preserved). |
| `delete_contact` | Delete a contact by UID. |
| `list_birthdays` | List upcoming birthdays sourced from contacts' `BDAY` field. |

Write tools are disabled when `READ_ONLY=true`. Times use ISO 8601; use
`YYYY-MM-DD` and `all_day: true` for whole-day events.

## Select a calendar

`CALDAV_USERNAME` is the identity used to authenticate to the CalDAV server.
For EGroupware, `CALDAV_CALENDAR_USER` optionally selects a different calendar
owner namespace; it does not change the authentication identity. If it is empty
or unset, the calendar owner defaults to `CALDAV_USERNAME`.

For example, with:

```env
CALDAV_URL=http://192.168.2.122:8082/egroupware/groupdav.php/
CALDAV_USERNAME=mardjor
CALDAV_PASSWORD=your-password
CALDAV_CALENDAR_USER=joao
DEFAULT_CALENDAR=Calendário joão
```

the MCP authenticates as `mardjor` and directly targets:

```text
http://192.168.2.122:8082/egroupware/groupdav.php/joao/calendar/
```

The authenticated user must have permission to access the target calendar in
EGroupware. `list_calendars` reports the configured owner's accessible calendar.
The Compose file forwards environment variables explicitly, so adding
`CALDAV_CALENDAR_USER` to `.env` alone is not enough. To use the setting with
Compose, uncomment the optional mapping under the service's `environment` section
in `docker-compose.yml`:

```yaml
CALDAV_CALENDAR_USER: ${CALDAV_CALENDAR_USER:-}
```

These two settings have different roles:

- `CALDAV_CALENDAR_USER` selects **whose EGroupware calendar namespace** to use.
- `DEFAULT_CALENDAR` selects **which calendar within that owner** to use when a
  tool call omits its `calendar` argument. Pass a display name or URL to a tool
  to select another calendar explicitly.

`ALLOWED_CALENDARS` remains a comma-separated allowlist of calendar display names
and applies to calendars under the selected owner.

## Contacts (CardDAV)

Contacts/address-book tools work alongside the calendar tools on the same
server, same credentials. `CALDAV_USERNAME` still authenticates; a separate
`CARDDAV_CONTACTS_USER` optionally selects the address-book owner, independent
of `CALDAV_CALENDAR_USER` — the calendar and contacts owners may be the same
EGroupware account or different ones. If `CARDDAV_CONTACTS_USER` is empty or
unset, it falls back to `CALDAV_USERNAME`.

For example, with:

```env
CALDAV_URL=http://192.168.2.122:8082/egroupware/groupdav.php/
CALDAV_USERNAME=mardjor
CALDAV_PASSWORD=your-password
CALDAV_CALENDAR_USER=joao
CARDDAV_CONTACTS_USER=joao
```

the MCP authenticates as `mardjor` and directly targets:

```text
http://192.168.2.122:8082/egroupware/groupdav.php/joao/addressbook/
```

`list_contact_books` discovers every address book under that owner (their own
plus any shared ones they have subscribed to in EGroupware's CalDAV
preferences); the other contact tools default to the owner's own
`addressbook/` when `address_book` is omitted. `list_contacts`/`search_contacts`
return structured fields (UID, name, organization, emails, phones, addresses,
birthday, notes, URL, categories) parsed from each vCard, not raw vCard text.

`update_contact` is read-modify-write: fields left unset are preserved exactly,
including list fields like emails/phones — "update the phone" never drops the
email, address, birthday, or notes. `delete_contact`/`update_contact` always
identify the contact by UID, never by name, so two contacts sharing a display
name are never confused; use `search_contacts` first to find the right UID.

`list_birthdays` reads the `BDAY` field directly from contacts — it does not
depend on EGroupware's own birthday calendar events, so it works even when
`CALDAV_CALENDAR_USER` and `CARDDAV_CONTACTS_USER` point at different accounts.
Without `start_date`/`end_date` it defaults to the next 30 days; an unknown
birth year (`--MM-DD`) is reported with `age: null` rather than a guessed age.

`READ_ONLY=true` disables `create_contact`/`update_contact`/`delete_contact`
exactly like it does the calendar write tools.

## Subscribed ICS calendars

Apple subscribed calendars (team schedules, holiday feeds, and similar) are
stored device-side and are not reachable over CalDAV. This server can pull their
underlying iCalendar documents from HTTP(S) URLs as a separate, read-only source:

```text
add_subscription(name="Team Schedule", url="webcal://example.com/team.ics")
```

`webcal://` URLs are rewritten to `https://`. The feed is fetched when added, and
its events are available through `list_events` and `get_event`. Details:

- Recurrence is expanded inside the queried time window.
- Feeds are cached for `ICS_CACHE_TTL` (default 15 minutes), then revalidated
  with `ETag` / `If-Modified-Since`.
- Subscriptions are always read-only and are identified by their id or URL when
  names collide.
- The feed list is persisted in `SUBSCRIPTIONS_FILE` (default
  `/data/subscriptions.json`). `SUBSCRIBED_ICS` can seed feeds at startup.
- Private, loopback, and link-local addresses are refused by default to prevent
  server-side request forgery. Set `ICS_ALLOW_PRIVATE_IPS=true` only when a
  LAN-hosted feed is needed.
- Feeds do not depend on CalDAV and remain readable when the CalDAV server is
  unreachable.

The persisted list can also be managed inside the running container:

```bash
docker compose exec -T caldav-mcp python subscriptions.py list
docker compose exec -T caldav-mcp python subscriptions.py add "Team Schedule" "webcal://example.com/team.ics"
docker compose exec -T caldav-mcp python subscriptions.py inspect "Team Schedule"
docker compose exec -T caldav-mcp python subscriptions.py remove "Team Schedule"
```

## Security

This server implements no authentication of its own. It must be gated by an
identity-aware authorization proxy and must not be exposed directly to the
internet. The intended topology is:

```text
edge tunnel → reverse proxy (TLS) → Pomerium (SSO + allowlist) → caldav-mcp
```

The Compose configuration publishes no host ports; the service is reachable on
the shared internal proxy network. Other protections include:

- `ALLOWED_CALENDARS` limits the calendars tools can access.
- `READ_ONLY=true` disables write tools and subscription management.
- `REQUIRE_POMERIUM_IDENTITY=true` optionally verifies Pomerium's signed identity
  assertion on each `/mcp` request. When enabled, configure
  `POMERIUM_JWKS_URL`, `POMERIUM_AUDIENCE`, and `pass_identity_headers: true` on
  the Pomerium route.
- The server checks MCP method/tool headers against the request body so proxy
  policies can safely use those headers.

## iCloud setup

1. Generate an app-specific password in Apple Account **Sign-In and Security**.
2. Set `CALDAV_USERNAME` to the Apple ID email and `CALDAV_PASSWORD` to that
   app-specific password.
3. Leave `CALDAV_URL` at `https://caldav.icloud.com/`; the client discovers the
   account's calendars.

App-specific passwords require two-factor authentication on the Apple account.

## Configuration

All configuration is provided through environment variables. See
[`.env.example`](.env.example) for the annotated list.

| Variable | Default | Notes |
| --- | --- | --- |
| `CALDAV_URL` | `https://caldav.icloud.com/` | CalDAV entry point. |
| `CALDAV_USERNAME` | — (required) | Authentication identity. |
| `CALDAV_PASSWORD` | — (required) | CalDAV password or app-specific password. |
| `CALDAV_CALENDAR_USER` | `CALDAV_USERNAME` | Optional EGroupware calendar owner; does not affect authentication. |
| `CARDDAV_CONTACTS_USER` | `CALDAV_USERNAME` | Optional EGroupware address-book owner; independent of `CALDAV_CALENDAR_USER`. |
| `DEFAULT_CALENDAR` | — | Calendar display name used when `calendar` is omitted. |
| `ALLOWED_CALENDARS` | — | Comma-separated calendar allowlist; empty allows all. |
| `READ_ONLY` | `false` | Disable calendar and subscription write operations. |
| `CALDAV_TIMEOUT` | `20` | Timeout in seconds for CalDAV requests. |
| `EXPAND_RECURRENCES` | `true` | Expand recurring events server-side; retry unexpanded if needed. |
| `UID_SCAN_WINDOW_DAYS` | `366` | Initial time range for fallback UID lookup. |
| `SUBSCRIPTIONS_FILE` | `/data/subscriptions.json` | Persisted ICS feed list. |
| `SUBSCRIBED_ICS` | — | Optional JSON map of feed names to URLs. |
| `ALLOWED_SUBSCRIPTIONS` | — | Comma-separated feed allowlist; empty allows all. |
| `ICS_TIMEOUT` | `20` | Timeout in seconds for fetching a feed. |
| `ICS_CACHE_TTL` | `900` | Seconds a fetched feed is reused before revalidation. |
| `ICS_ALLOW_PRIVATE_IPS` | `false` | Permit feeds on private/LAN addresses. |
| `LOG_HEALTHZ` | `false` | Log health-check access lines. |
| `STARTUP_TEST` | `false` | Connect and list calendars at startup to verify configuration. |
| `MCP_ALLOWED_HOSTS` | — | Allowed `Host` headers; set to the proxy route host to enable the guard. |
| `MCP_ALLOWED_ORIGINS` | — | Allowed `Origin` headers; defaults from allowed hosts. |
| `TOOLS_LIST_TTL_MS` | `3600000` | MCP tools-list cache hint lifetime in milliseconds. |

## Run

```bash
cp .env.example .env
# Fill in CALDAV_USERNAME and CALDAV_PASSWORD, then:
docker compose up -d
```

The image is published to `ghcr.io/jb09/caldav-mcp-wrapper:latest`.