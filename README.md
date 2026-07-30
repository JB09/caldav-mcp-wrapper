# caldav-mcp-wrapper

A minimal, self-hosted [MCP](https://modelcontextprotocol.io/) server that exposes
**read and write** tools for a CalDAV calendar — designed for Apple **iCloud**
(via an app-specific password), and compatible with any CalDAV server.

It is a CalDAV counterpart to
[smtp-mcp-wrapper](https://github.com/JB09/smtp-mcp-wrapper) and follows the same
deployment and security model.

## Tools

Read:

| Tool | Purpose |
| --- | --- |
| `list_calendars` | List the calendars in the account (respecting the allowlist). |
| `list_events` | List events in a calendar within a start/end window. |
| `get_event` | Fetch a single event by UID. |

Write (disabled when `READ_ONLY=true`):

| Tool | Purpose |
| --- | --- |
| `create_event` | Create an event (timed or all-day). |
| `update_event` | Update fields of an existing event by UID. |
| `delete_event` | Delete an event by UID. |

Subscribed ICS feeds (see below):

| Tool | Purpose |
| --- | --- |
| `list_subscriptions` | List the subscribed feeds and their last fetch result. |
| `add_subscription` | Subscribe to an ICS feed URL (validated on add). |
| `remove_subscription` | Stop serving a feed (by id, URL, or name). |

Times are ISO 8601. Use `YYYY-MM-DD` with `all_day: true` for whole-day events.

## Subscribed ICS calendars

Apple **"subscribed calendars"** (team/league schedules, holiday feeds) are stored
device-side and are **not reachable over CalDAV** — they never appear in
`list_calendars` and nothing you configure on the CalDAV side will surface them.

The underlying data is just an iCalendar document at an HTTP(S) URL, so this
server can pull those URLs directly as a second, **read-only** source:

```
add_subscription(name="Team Schedule", url="webcal://example.com/team.ics")
```

`webcal://` links (what Apple hands out) are rewritten to `https://`. The feed is
fetched once at add-time so a bad URL fails immediately rather than silently
returning nothing later. After that the feed's events are readable through the
normal `list_events` / `get_event` tools, and `list_calendars` reports it with
`"kind": "subscription"` and `"read_only": true`.

Details worth knowing:

- **Recurrence is expanded.** Team schedules lean on `RRULE`; occurrences are
  expanded within the queried window so a weekly practice appears on every date.
- **Feeds are cached** for `ICS_CACHE_TTL` (default 15 min) and then revalidated
  with `ETag`/`If-Modified-Since` rather than re-downloaded.
- **Always read-only.** `create_event`/`update_event`/`delete_event` reject a
  subscription target with a clear error.
- **Identity is the feed URL/id**, not the display name — pass the `id` or URL
  from `list_calendars` when names collide.
- **Persistence:** the pull list is stored at `SUBSCRIPTIONS_FILE`
  (default `/data/subscriptions.json`) on the `caldav_mcp_data` volume, so feeds
  added at runtime survive restarts and image updates. Declare feeds up front with
  `SUBSCRIBED_ICS` if you prefer config over the tool.
- **SSRF guard:** `add_subscription` fetches an arbitrary URL, so private,
  loopback and link-local targets are refused (every redirect hop is re-checked).
  Set `ICS_ALLOW_PRIVATE_IPS=true` only to subscribe to a LAN-hosted feed.
- **Feeds do not depend on CalDAV.** If iCloud is unreachable, subscribed feeds
  stay readable.

### Managing feeds without the MCP tools

The MCP tools are the normal path, but they only work when the server *and* the
proxy in front of it are healthy. The same pull list can be managed from the
command line as a backup — it uses the same file and lock, so it works while the
server is running and changes take effect immediately (no restart):

```bash
docker compose exec -T caldav-mcp python subscriptions.py list
docker compose exec -T caldav-mcp python subscriptions.py add "Team Schedule" "webcal://example.com/team.ics"
docker compose exec -T caldav-mcp python subscriptions.py inspect "Team Schedule"
docker compose exec -T caldav-mcp python subscriptions.py remove "Team Schedule"
```

`add` validates by fetching the feed, the same as the tool, and reports its size
and event count; pass `--no-validate` to add a feed that is temporarily
unreachable. `inspect` re-fetches and shows what a feed actually contains — size,
`VEVENT` count, and the next occurrences — which is how you tell a broken URL from
a valid feed whose schedule simply isn't published yet. `remove` exits non-zero if
nothing matched. All three accept an id, a URL, or a display name.

Adding and removing feeds is logged at INFO, so `docker compose logs caldav-mcp`
shows why a feed appeared or vanished no matter which path changed it.

To declare feeds up front instead, set `SUBSCRIBED_ICS` to a JSON `{"name": "url"}`
map — it is merged into the pull list at startup (additive: it never removes
feeds added another way, and it never fetches, so a dead feed cannot block boot).

## Security architecture — read this first

**This server implements no authentication of its own, by design.** It MUST be
gated by an authorization service. Do not expose it directly to the internet.

The intended topology keeps the server on an internal network only, with every
external request flowing through an identity-aware proxy:

```
edge tunnel → reverse proxy (TLS) → Pomerium (SSO + allowlist to a single identity) → caldav-mcp-wrapper
```

Any equivalent identity-aware proxy works (Cloudflare Access, oauth2-proxy, etc.).
`docker-compose.yml` deliberately publishes **no host ports**: the container is
reachable only over the internal `proxy` network by container name.

Defense-in-depth beyond the proxy:

- **Calendar allowlist** — `ALLOWED_CALENDARS` hard-limits which calendars any tool
  can touch, so even a misused tool cannot reach other calendars.
- **Read-only mode** — `READ_ONLY=true` disables all write tools.
- **Optional Pomerium identity verification** — set `REQUIRE_POMERIUM_IDENTITY=true`
  to cryptographically verify Pomerium's identity assertion (signature + expiry +
  audience) on every `/mcp` request against Pomerium's JWKS. This blocks anything
  on the shared Docker network from bypassing Pomerium and reaching the app
  directly. When enabled, set `pass_identity_headers: true` on the Pomerium route
  and provide `POMERIUM_JWKS_URL` and `POMERIUM_AUDIENCE`.

## iCloud setup

1. Sign in to [account.apple.com](https://account.apple.com) → **Sign-In and
   Security** → **App-Specific Passwords** → generate one for this server.
2. Set `CALDAV_USERNAME` to your Apple ID email and `CALDAV_PASSWORD` to that
   app-specific password.
3. Leave `CALDAV_URL` at the default `https://caldav.icloud.com/`; the client
   discovers your calendars from there.

App-specific passwords require two-factor authentication on your Apple ID.

## Configuration

All configuration is via environment variables — see [`.env.example`](.env.example)
for the full annotated list. Secrets are injected at runtime and never baked into
the image. Key variables:

| Variable | Default | Notes |
| --- | --- | --- |
| `CALDAV_URL` | `https://caldav.icloud.com/` | CalDAV entry point. |
| `CALDAV_USERNAME` | — (required) | Apple ID / CalDAV username. |
| `CALDAV_PASSWORD` | — (required) | App-specific password. |
| `DEFAULT_CALENDAR` | — | Calendar used when `calendar` is omitted. |
| `ALLOWED_CALENDARS` | — | Comma-separated allowlist; empty = all. |
| `READ_ONLY` | `false` | Disable write tools (incl. subscription management) when `true`. |
| `SUBSCRIPTIONS_FILE` | `/data/subscriptions.json` | Persisted ICS pull list; must be on a volume. |
| `SUBSCRIBED_ICS` | — | Optional JSON `{"name": "url"}` seed merged at startup. |
| `ALLOWED_SUBSCRIPTIONS` | — | Comma-separated allowlist of feed names/ids; empty = all. |
| `ICS_CACHE_TTL` | `900` | Seconds a fetched feed is reused before revalidating. |
| `ICS_ALLOW_PRIVATE_IPS` | `false` | Allow feeds on private/LAN addresses (SSRF guard off). |
| `LOG_HEALTHZ` | `false` | Log `/healthz` access lines (noisy; off by default). |
| `STARTUP_TEST` | `false` | Connect and list calendars at startup to verify config. |
| `MCP_ALLOWED_HOSTS` | — | Allowed `Host` headers (DNS-rebinding guard). Empty = guard off, any host accepted. Set to your Pomerium route host to enable. |
| `MCP_ALLOWED_ORIGINS` | — | Allowed `Origin` headers. Defaults to `https://` + each allowed host. |

## Run

```bash
cp .env.example .env      # fill in CALDAV_USERNAME / CALDAV_PASSWORD etc.
docker compose up -d
```

The image is built and published to GHCR by CI
(`ghcr.io/jb09/caldav-mcp-wrapper:latest`).

## Maintenance

- **Dependabot** opens weekly PRs for the Python deps, the Docker base image, and
  the GitHub Actions used in CI.
- **CI (`build` workflow)** builds the image on every push/PR, pushes to GHCR on
  `main`, and does a weekly no-cache rebuild so OS/Python security patches land
  even without code changes.
- **Smoke test** (`scripts/smoke_test.sh`, run by CI before the push step) starts
  the built image and drives a real MCP `initialize` + `tools/list` against it
  using a non-localhost `Host` header, then checks that `MCP_ALLOWED_HOSTS`
  accepts the route host and rejects others. A build alone cannot catch a server
  that binds the wrong interface or answers `421` to proxied requests — both keep
  `/healthz` green. Run it locally with `docker build -t caldav-mcp:smoke . &&
  ./scripts/smoke_test.sh caldav-mcp:smoke`.
- **Auto-merge** (`dependabot-automerge` workflow) enables auto-merge for
  patch/minor Dependabot bumps once required checks pass; major bumps are left for
  manual review.
- **Watchtower** (opt-in label in compose) pulls refreshed images automatically.
