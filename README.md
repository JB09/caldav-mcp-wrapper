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

Times are ISO 8601. Use `YYYY-MM-DD` with `all_day: true` for whole-day events.

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
| `READ_ONLY` | `false` | Disable write tools when `true`. |
| `STARTUP_TEST` | `false` | Connect and list calendars at startup to verify config. |

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
- **Auto-merge** (`dependabot-automerge` workflow) enables auto-merge for
  patch/minor Dependabot bumps once required checks pass; major bumps are left for
  manual review.
- **Watchtower** (opt-in label in compose) pulls refreshed images automatically.
