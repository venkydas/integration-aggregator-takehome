#!/usr/bin/env bash
# Interactive demo of the three flows against REAL GitHub.
# Needs a GitHub OAuth App (callback http://localhost:8080/callback) and its
# credentials in .env as GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET.
#
# Everything printed is also written to demo/github-demo.txt. Secrets never
# appear: the client secret is sent but not shown, and tokens are masked.
set -euo pipefail

APP_NS="${APP_NAMESPACE:-aggregator}"
A="http://localhost:8080"
USER_ID="${DEMO_USER:-demo-user}"
OUT=demo/github-demo.txt

[[ -f .env ]] || { echo "missing .env (GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET)"; exit 1; }
set -a; . ./.env; set +a
: "${GITHUB_CLIENT_ID:?not set in .env}" "${GITHUB_CLIENT_SECRET:?not set in .env}"

mkdir -p demo
# Mirror the session into the transcript, without colour codes.
exec > >(tee >(sed -u 's/\x1b\[[0-9;]*m//g' >"$OUT")) 2>&1

step() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
run()  { printf '\033[2m$ %s\033[0m\n' "$*"; }
mask() { local t=$1; printf '%s…%s (%d chars)' "${t:0:4}" "${t: -2}" "${#t}"; }

if ! curl -sf -o /dev/null "$A/healthz"; then
  kubectl -n "$APP_NS" port-forward svc/aggregator 8080:8080 >/dev/null 2>&1 &
  PF=$!
  trap 'kill $PF 2>/dev/null || true' EXIT
  for _ in $(seq 1 20); do curl -sf -o /dev/null "$A/healthz" && break; sleep 1; done
fi

step "Integration aggregator demo against real GitHub — $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "commit $(git rev-parse --short HEAD); service at $A"
kubectl get pods -A --no-headers | awk '$2 ~ /^(openbao|aggregator)/ {print "  " $1 "/" $2 "  " $4}'

step "1. Register GitHub as a provider (the secret goes to OpenBao only)"
run "curl -X POST $A/providers -d '{\"name\":\"github\",\"provider\":\"github\",\"client_id\":\"$GITHUB_CLIENT_ID\",\"client_secret\":\"<from .env, not shown>\"}'"
curl -s -X POST "$A/providers" -H 'content-type: application/json' \
  -d "{\"name\":\"github\",\"provider\":\"github\",\"client_id\":\"$GITHUB_CLIENT_ID\",\"client_secret\":\"$GITHUB_CLIENT_SECRET\"}" | jq .

step "2. Connect user '$USER_ID': get the GitHub authorization URL"
run "curl -X POST $A/providers/github/users/$USER_ID/connect"
auth_url=$(curl -s -X POST "$A/providers/github/users/$USER_ID/connect" | jq -r .authorization_url)
echo "$auth_url"
echo
echo "Opening it in your browser — approve the app on GitHub."
echo "GitHub then redirects the browser to $A/callback, where OpenBao exchanges the code."
# explorer.exe often exits non-zero even when the browser opened.
explorer.exe "$auth_url" 2>/dev/null || true
echo "(if no browser window opened, copy the URL above into one)"
read -r -p "Press Enter once the browser shows {\"status\":\"connected\",...} " _ </dev/tty

step "3. Ask for the token: 202 + Location, then poll"
run "curl -i $A/github/$USER_ID"
headers=$(curl -s -D - -o /dev/null "$A/github/$USER_ID")
echo "$headers" | grep -iE '^(HTTP|location)' | tr -d '\r'
location=$(echo "$headers" | awk 'tolower($1)=="location:" {print $2}' | tr -d '\r')
for _ in $(seq 1 30); do
  body=$(curl -s "$A$location")
  [[ $(jq -r .status <<<"$body") != pending ]] && break
  sleep 0.3
done
run "curl $A$location"
token=$(jq -r '.access_token // empty' <<<"$body")
jq --arg t "$( [[ -n $token ]] && mask "$token" )" '.access_token = $t' <<<"$body"
[[ -n $token ]] || { echo "no token — see the response above"; exit 1; }

step "4. Prove the token is real: call the GitHub API with it"
run "curl -H 'Authorization: Bearer <token>' https://api.github.com/user"
curl -s -H "Authorization: Bearer $token" https://api.github.com/user | jq '{login, id, html_url}'

step "5. Ask again: same token, served by OpenBao without a new login"
location=$(curl -s -D - -o /dev/null "$A/github/$USER_ID" | awk 'tolower($1)=="location:" {print $2}' | tr -d '\r')
sleep 0.5
again=$(curl -s "$A$location" | jq -r .access_token)
[[ $again == "$token" ]] && echo "same token: $(mask "$again")" || echo "different token: $(mask "$again")"

step "6. The service never stores tokens itself"
echo "Tokens live only in OpenBao (oauth2/creds/github/$USER_ID); the service keeps"
echo "request results in memory for 5 minutes. A user who never connected:"
location=$(curl -s -D - -o /dev/null "$A/github/nobody" | awk 'tolower($1)=="location:" {print $2}' | tr -d '\r')
sleep 0.5
curl -s "$A$location" | jq '{status, error_code, error}'

unset token again GITHUB_CLIENT_SECRET
step "Done — transcript saved to $OUT"
