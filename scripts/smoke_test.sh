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
# So this asserts what a real client does: an MCP `initialize` + `tools/list`
# over the published port, carrying a *non-localhost* Host header the way
# Pomerium would. No CalDAV account is needed — neither call touches the backend.
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

echo "==> smoke test passed"
