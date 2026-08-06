#!/usr/bin/env bash
# Runtime smoke test for the built image.
#
# `docker build` only proves the image assembles — it cannot catch the failures
# that matter most here, because they are silent:
#
#   * MCP SDK 2.x moved host/port off the constructor onto run(). Omit them and
#     the server binds 127.0.0.1:8000 instead of 0.0.0.0:8080 — builds clean,
#     starts clean, unreachable.
#   * SDK 2.x enables DNS-rebinding protection by default. Get the Host allowlist
#     wrong and every proxied request gets 421 while /healthz still returns 200,
#     so the container reports healthy with every tool call failing.
#
#   * The SDK routes per request on the MCP-Protocol-Version header, so legacy
#     (handshake + session) and modern 2026-07-28 (single stateless POST)
#     clients are served by different code. Exercising one proves nothing about
#     the other.
#
# So this asserts what a real client does — in both protocol eras: an MCP
# `initialize` + `tools/list` over the published port, carrying a *non-localhost*
# Host header the way Pomerium would, and a sessionless 2026-07-28 `tools/list`.
# No CalDAV account is needed — none of those calls touch the backend.
set -euo pipefail

IMAGE="${1:-caldav-mcp:smoke}"
PORT="${SMOKE_PORT:-8931}"
# Stands in for the Pomerium route host; the container must accept it.
ROUTE_HOST="${SMOKE_ROUTE_HOST:-caldav-mcp.example.com}"
BASE="http://127.0.0.1:$PORT"
NAME=""

cleanup() {
  [ -n "$NAME" ] && docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  [ -n "$NAME" ] && docker logs "$NAME" >&2 || true
  exit 1
}

# start_server [extra docker -e flags...]
start_server() {
  cleanup
  NAME="caldav-mcp-smoke-$$"
  docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8080" \
    -e CALDAV_USERNAME=smoke -e CALDAV_PASSWORD=smoke \
    "$@" "$IMAGE" >/dev/null
  for _ in $(seq 1 60); do
    curl -fsS -o /dev/null "$BASE/healthz" 2>/dev/null && return 0
    sleep 1
  done
  fail "server never answered on $BASE/healthz — wrong bind interface or port?"
}

# post_mcp <host-header> [extra curl args...]
# Sets $STATUS and $BODY. Deliberately does not print the body: callers would
# need a command substitution, whose subshell would discard $STATUS.
post_mcp() {
  local host="$1"; shift
  curl -sS -D /tmp/smoke_headers -o /tmp/smoke_body -X POST "$BASE/mcp" \
    -H "Host: $host" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    "$@"
  STATUS=$(awk 'NR==1{print $2}' /tmp/smoke_headers)
  BODY=$(cat /tmp/smoke_body)
}

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}'

# --- Phase 1: default config — full MCP handshake over a proxied Host header ---
echo "==> phase 1: default config ($IMAGE)"
start_server

echo "  - GET /healthz"
curl -fsS "$BASE/healthz" | grep -qx "ok" || fail "/healthz did not return ok"

# /healthz passing proves nothing about /mcp: the Host-header guard rejects /mcp
# while leaving /healthz untouched, which is exactly the silent-breakage case.
echo "  - POST /mcp initialize (Host: $ROUTE_HOST)"
post_mcp "$ROUTE_HOST" -d "$INIT"
if [ "$STATUS" != "200" ]; then
  [ "$STATUS" = "421" ] && echo "  -> 421: SDK Host allowlist rejected '$ROUTE_HOST'; see _transport_security()" >&2
  fail "initialize returned HTTP $STATUS (expected 200): $BODY"
fi
grep -q '"serverInfo"' <<<"$BODY" || fail "initialize response missing serverInfo: $BODY"

session=$(grep -i '^mcp-session-id:' /tmp/smoke_headers | tr -d '\r' | awk '{print $2}')
[ -n "$session" ] || fail "no mcp-session-id header returned by initialize"

echo "  - POST /mcp tools/list"
post_mcp "$ROUTE_HOST" -H "mcp-session-id: $session" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'
post_mcp "$ROUTE_HOST" -H "mcp-session-id: $session" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
[ "$STATUS" = "200" ] || fail "tools/list returned HTTP $STATUS: $BODY"

# Every tool the README documents must be advertised; a partial registration
# (e.g. a decorator that silently stopped applying) should fail the build.
for tool in list_calendars list_events get_event create_event update_event \
            delete_event add_subscription list_subscriptions remove_subscription; do
  grep -q "\"$tool\"" <<<"$BODY" || fail "tools/list is missing '$tool': $BODY"
done
echo "  OK: healthz + initialize + all 9 tools"

# --- Phase 2: the recommended production posture — guard ON ------------------
# MCP_ALLOWED_HOSTS is what operators are told to set, so prove the control works
# in both directions rather than only testing the permissive default.
echo "==> phase 2: MCP_ALLOWED_HOSTS=$ROUTE_HOST"
start_server -e "MCP_ALLOWED_HOSTS=$ROUTE_HOST"

echo "  - allowed Host is accepted"
post_mcp "$ROUTE_HOST" -d "$INIT"
[ "$STATUS" = "200" ] || fail "allowed host '$ROUTE_HOST' got HTTP $STATUS (expected 200): $BODY"

echo "  - foreign Host is rejected"
post_mcp "evil.example.com" -d "$INIT"
[ "$STATUS" = "421" ] || fail "foreign host got HTTP $STATUS (expected 421) — guard not enforcing"
echo "  OK: guard accepts the route host and rejects others"

# --- Phase 3: ICS feed fetching -------------------------------------------
# subscriptions.py fetches feeds with httpx2, which verifies TLS against the
# *system* trust store rather than a bundled certifi CA set. That makes feed
# fetching depend on the base image shipping ca-certificates — a dependency a
# base-image bump could silently drop. Assert the store loads, then exercise the
# real fetch path end to end. Runs inside the container against a server bound to
# its own loopback, so it needs no network egress and cannot flake.
echo "==> phase 3: ICS fetch (httpx2 + system trust store)"
docker exec -e ICS_ALLOW_PRIVATE_IPS=true -e SUBSCRIPTIONS_FILE=/tmp/smoke-subs.json \
  "$NAME" python - <<'PY' || fail "ICS fetch check failed (see output above)"
import ssl, threading, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

import truststore  # noqa: F401  -- httpx2's default verifier must be importable

# truststore resolves the OS store lazily and cannot enumerate it, so check the
# OpenSSL default paths it delegates to on Linux: empty here means the image has
# no ca-certificates and every HTTPS feed fetch would fail to verify.
n = len(ssl.create_default_context().get_ca_certs())
if n == 0:
    sys.exit(f"system trust store is empty ({ssl.get_default_verify_paths()}) — base "
             "image is missing ca-certificates, so HTTPS feed fetching would fail")
print(f"  - system trust store OK ({n} CAs)")

ICS = (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//smoke//EN\r\nBEGIN:VEVENT\r\n"
       b"UID:smoke-1\r\nDTSTART:20260801T100000Z\r\nDTEND:20260801T110000Z\r\n"
       b"SUMMARY:Smoke\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        # Exercise the redirect branch too: it relies on URL.join(), which is the
        # part of the client API most likely to drift between implementations.
        if self.path == "/redirect":
            self.send_response(302); self.send_header("Location", "/feed.ics"); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar")
        self.send_header("ETag", '"smoke"')
        self.send_header("Content-Length", str(len(ICS)))
        self.end_headers(); self.wfile.write(ICS)

srv = HTTPServer(("127.0.0.1", 8099), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

import subscriptions
for label, url in (("direct", "http://127.0.0.1:8099/feed.ics"),
                   ("redirect", "http://127.0.0.1:8099/redirect")):
    text = subscriptions.fetch(url, force=True)
    event = subscriptions.parse(text).walk("VEVENT")[0]
    if str(event["SUMMARY"]) != "Smoke":
        sys.exit(f"{label}: unexpected event {event['SUMMARY']!r}")
    print(f"  - {label} fetch + parse OK")
PY
echo "  OK: feeds fetch and parse"

# --- Phase 4: the modern (MCP 2026-07-28) stateless request path ------------
# Everything above drives the *legacy* route: an `initialize` handshake at
# protocolVersion 2025-06-18 followed by requests carrying its Mcp-Session-Id.
# The SDK picks the era per request from the MCP-Protocol-Version header, so a
# 2026-07-28 client takes an entirely different code path — one self-contained
# POST, no handshake, no session — that none of the phases above touch. Without
# this, an SDK bump could break every modern client while CI stays green, which
# is the exact class of silent breakage this script exists to catch.
#
# Reuses the phase-2/3 container, so this also proves the modern path works with
# the Host guard on — the posture operators are told to run.
echo "==> phase 4: modern stateless path (MCP-Protocol-Version: 2026-07-28)"

MODERN_VERSION="2026-07-28"
# With no handshake, everything the server used to learn from `initialize` rides
# along on each request instead, in a `params._meta` envelope. All three keys are
# required — omit them and the server answers 400 (-32602), which is precisely
# the kind of protocol drift this phase is here to notice.
META='"_meta":{'
META+='"io.modelcontextprotocol/protocolVersion":"'$MODERN_VERSION'",'
META+='"io.modelcontextprotocol/clientCapabilities":{},'
META+='"io.modelcontextprotocol/clientInfo":{"name":"smoke","version":"1"}}'

echo "  - tools/list with no session"
post_mcp "$ROUTE_HOST" \
  -H "MCP-Protocol-Version: $MODERN_VERSION" \
  -H "Mcp-Method: tools/list" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{$META}}"
[ "$STATUS" = "200" ] || fail "modern tools/list returned HTTP $STATUS (expected 200): $BODY"

for tool in list_calendars list_events get_event create_event update_event \
            delete_event add_subscription list_subscriptions remove_subscription; do
  grep -q "\"$tool\"" <<<"$BODY" || fail "modern tools/list is missing '$tool': $BODY"
done

# The whole point of the modern path is that there is no protocol session to
# store; a session id coming back would mean the request fell through to the
# legacy handler and the era routing is broken.
if grep -qi '^mcp-session-id:' /tmp/smoke_headers; then
  fail "modern request returned an mcp-session-id header — it was served by the legacy path"
fi

# server.py declares a cache hint for tools/list; assert it survives to the wire,
# since a client that never sees it silently re-fetches the catalog every time.
grep -q '"ttlMs"' <<<"$BODY" || fail "modern tools/list result is missing ttlMs: $BODY"
grep -q '"cacheScope":"public"' <<<"$BODY" || fail "modern tools/list result is missing cacheScope: $BODY"
echo "  OK: 9 tools, no session, cache hint present"

# The 2026-07-28 spec requires Mcp-Method (and Mcp-Name) to mirror the body so
# gateways can route on headers alone; the SDK rejects a mismatch with
# HEADER_MISMATCH (-32020). That guarantee is what makes per-tool proxy policy
# safe to write, so prove it holds — and that a proxy rewriting those headers
# would fail loudly rather than silently bypassing such a policy.
echo "  - Mcp-Method disagreeing with the body is rejected"
post_mcp "$ROUTE_HOST" \
  -H "MCP-Protocol-Version: $MODERN_VERSION" \
  -H "Mcp-Method: tools/list" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"list_calendars\",\"arguments\":{},$META}}"
[ "$STATUS" = "400" ] || fail "header/body mismatch got HTTP $STATUS (expected 400): $BODY"
grep -q '\-32020' <<<"$BODY" || fail "expected HEADER_MISMATCH (-32020) in: $BODY"
echo "  OK: header/body agreement enforced"

echo "==> smoke test passed"
