#!/usr/bin/env bash
# End-to-end test against the deployed stack, using mock-oauth2-server as the
# OAuth provider: register -> connect -> login -> callback -> async token.
#
# The provider's URLs use its in-cluster name, because OpenBao must reach it at
# the same address the browser uses. From this machine we keep that hostname but
# route it through a port-forward with curl --connect-to.
set -euo pipefail

APP_NS="${APP_NAMESPACE:-aggregator}"
MOCK_NS="${MOCK_NAMESPACE:-mock-oauth2}"
MOCK_HOST="mock-oauth2.${MOCK_NS}.svc:8080"
APP_PORT=8080
MOCK_PORT=18080
A="http://localhost:${APP_PORT}"
PROVIDER=mock
USER_ID=smoke-user

pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }

kubectl -n "$APP_NS" port-forward svc/aggregator "$APP_PORT:8080" >/dev/null 2>&1 &
PF_APP=$!
kubectl -n "$MOCK_NS" port-forward svc/mock-oauth2 "$MOCK_PORT:8080" >/dev/null 2>&1 &
PF_MOCK=$!
trap 'kill $PF_APP $PF_MOCK 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  curl -sf -o /dev/null "$A/healthz" &&
    curl -sf -o /dev/null "http://localhost:$MOCK_PORT/default/.well-known/openid-configuration" &&
    break
  sleep 1
done

echo "smoke test against $A"

# 1. Register the provider. The plugin's generic OIDC provider discovers the
#    endpoints from the issuer URL.
code=$(curl -s -o /tmp/smoke.out -w '%{http_code}' -X POST "$A/providers" \
  -H 'content-type: application/json' \
  -d "{\"name\":\"$PROVIDER\",\"provider\":\"oidc\",\"client_id\":\"smoke-client\",
       \"client_secret\":\"smoke-secret\",
       \"provider_options\":{\"issuer_url\":\"http://$MOCK_HOST/default\"}}")
[[ $code == 201 ]] || fail "register provider: HTTP $code $(cat /tmp/smoke.out)"
pass "register provider -> 201"

# 2. Connect: get the authorization URL.
auth_url=$(curl -sf -X POST "$A/providers/$PROVIDER/users/$USER_ID/connect" | jq -r .authorization_url)
[[ $auth_url == "http://$MOCK_HOST/"* ]] || fail "connect: unexpected URL '$auth_url'"
pass "connect -> authorization URL"

# 3. "Log in": the mock approves at once and redirects to our callback.
callback_url=$(curl -s -o /dev/null -w '%{redirect_url}' \
  --connect-to "${MOCK_HOST}:127.0.0.1:${MOCK_PORT}" "$auth_url")
[[ $callback_url == "$A/callback?"* ]] || fail "login: expected redirect to callback, got '$callback_url'"
pass "provider login -> redirect to /callback"

# 4. Callback: OpenBao exchanges the code for tokens.
code=$(curl -s -o /tmp/smoke.out -w '%{http_code}' "$callback_url")
[[ $code == 200 ]] || fail "callback: HTTP $code $(cat /tmp/smoke.out)"
pass "callback -> connected"

# 5. Replaying the same callback must be refused (single-use state).
code=$(curl -s -o /dev/null -w '%{http_code}' "$callback_url")
[[ $code == 400 ]] || fail "callback replay: expected 400, got $code"
pass "callback replay -> 400"

# 6. Async token retrieval: 202 + Location, then poll.
location=$(curl -s -D - -o /dev/null "$A/$PROVIDER/$USER_ID" |
  awk 'tolower($1)=="location:" {print $2}' | tr -d '\r')
[[ $location == /requests/* ]] || fail "token request: no Location header"
pass "token request -> 202 Location: $location"

for _ in $(seq 1 30); do
  body=$(curl -sf "$A$location")
  [[ $(jq -r .status <<<"$body") != pending ]] && break
  sleep 0.5
done
[[ $(jq -r .status <<<"$body") == done ]] || fail "token poll: $body"
token=$(jq -r .access_token <<<"$body")
[[ -n $token && $token != null ]] || fail "token poll: empty access_token"
pass "poll -> done, access_token received (${#token} chars)"

# 7. A user who never connected gets a clean failure, not a hang.
location=$(curl -s -D - -o /dev/null "$A/$PROVIDER/nobody" |
  awk 'tolower($1)=="location:" {print $2}' | tr -d '\r')
for _ in $(seq 1 30); do
  body=$(curl -sf "$A$location")
  [[ $(jq -r .status <<<"$body") != pending ]] && break
  sleep 0.5
done
[[ $(jq -r .error_code <<<"$body") == not_connected ]] || fail "unconnected user: $body"
pass "unconnected user -> failed / not_connected"

echo "smoke test passed"
