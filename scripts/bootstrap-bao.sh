#!/usr/bin/env bash
# Initialise, unseal and configure OpenBao running in the cluster.
# Safe to run any number of times: every step checks current state first.
#
# Unseal key + root token are kept in a Kubernetes Secret (never on disk, never
# printed). Fine for a local cluster; production would use auto-unseal (KMS).
set -euo pipefail

NS="${BAO_NAMESPACE:-openbao}"
POD="${BAO_POD:-openbao-0}"
INIT_SECRET="openbao-init"
APP_NS="${APP_NAMESPACE:-aggregator}"
APP_SA="${APP_SERVICE_ACCOUNT:-aggregator}"

log() { printf '[bootstrap] %s\n' "$*"; }

# Run the bao CLI inside the pod without a token.
bao_anon() { kubectl -n "$NS" exec "$POD" -- bao "$@"; }

# Run the bao CLI inside the pod as root. The token is passed on stdin so it
# never shows up in process listings or shell history.
bao_root() {
  printf '%s' "$ROOT_TOKEN" |
    kubectl -n "$NS" exec -i "$POD" -- sh -c 'BAO_TOKEN=$(cat) exec bao "$@"' -- "$@"
}

secret_field() {
  kubectl -n "$NS" get secret "$INIT_SECRET" -o "jsonpath={.data.$1}" | base64 -d
}

log "waiting for pod $POD to be running"
# `kubectl wait` fails if the pod does not exist yet (fresh install), so wait for it to appear first.
for _ in $(seq 1 60); do
  kubectl -n "$NS" get pod "$POD" >/dev/null 2>&1 && break
  sleep 2
done
kubectl -n "$NS" wait pod "$POD" --for=jsonpath='{.status.phase}'=Running --timeout=180s >/dev/null
# The listener needs a moment after the container starts.
for _ in $(seq 1 30); do
  # `bao status` exits 2 when sealed, so ignore its exit code and read the JSON.
  STATUS=$(bao_anon status -format=json 2>/dev/null || true)
  [[ -n "$STATUS" ]] && break
  sleep 2
done
[[ -n "$STATUS" ]] || { log "OpenBao did not respond"; exit 1; }

# --- 1. Initialise (once) -----------------------------------------------------
if [[ "$(jq -r .initialized <<<"$STATUS")" != "true" ]]; then
  log "initialising"
  INIT_JSON=$(bao_anon operator init -key-shares=1 -key-threshold=1 -format=json)
  kubectl -n "$NS" create secret generic "$INIT_SECRET" \
    --from-literal=unseal_key="$(jq -r '.unseal_keys_b64[0]' <<<"$INIT_JSON")" \
    --from-literal=root_token="$(jq -r .root_token <<<"$INIT_JSON")" >/dev/null
  unset INIT_JSON
  STATUS=$(bao_anon status -format=json 2>/dev/null || true)
else
  log "already initialised"
fi

# --- 2. Unseal (after every pod restart) ---------------------------------------
if [[ "$(jq -r .sealed <<<"$STATUS")" == "true" ]]; then
  log "unsealing"
  secret_field unseal_key |
    kubectl -n "$NS" exec -i "$POD" -- sh -c 'exec bao operator unseal "$(cat)"' >/dev/null
else
  log "already unsealed"
fi

ROOT_TOKEN=$(secret_field root_token)

# --- 3. Register the oauthapp plugin -------------------------------------------
WANT_SHA=$(kubectl -n "$NS" exec "$POD" -- cat /openbao/oauthapp.sha256)
HAVE_SHA=$(bao_root plugin info -format=json secret oauthapp 2>/dev/null | jq -r '.sha256 // empty' || true)
if [[ "$WANT_SHA" != "$HAVE_SHA" ]]; then
  log "registering oauthapp plugin"
  bao_root plugin register -sha256="$WANT_SHA" secret oauthapp >/dev/null
  # A changed binary (new image) needs the running mount reloaded.
  [[ -n "$HAVE_SHA" ]] && bao_root plugin reload -plugin=oauthapp >/dev/null
else
  log "plugin already registered"
fi

# --- 4. Mount it at oauth2/ ------------------------------------------------------
if ! bao_root secrets list -format=json | jq -e 'has("oauth2/")' >/dev/null; then
  log "enabling oauthapp at oauth2/"
  bao_root secrets enable -path=oauth2 oauthapp >/dev/null
else
  log "oauth2/ already mounted"
fi

# --- 5. Kubernetes auth so the aggregator logs in with its service account -----
if ! bao_root auth list -format=json | jq -e 'has("kubernetes/")' >/dev/null; then
  log "enabling kubernetes auth"
  bao_root auth enable kubernetes >/dev/null
else
  log "kubernetes auth already enabled"
fi
# The writes below are declarative upserts: same input, same state.
bao_root write auth/kubernetes/config kubernetes_host=https://kubernetes.default.svc >/dev/null

# Least privilege: the service can manage providers and credentials under
# oauth2/, but cannot read anything else and cannot change OpenBao itself.
POLICY='
path "oauth2/servers/*" {
  capabilities = ["create", "update", "read"]
}
path "oauth2/auth-code-url" {
  capabilities = ["update"]
}
path "oauth2/creds/*" {
  capabilities = ["create", "update", "read"]
}'
# The policy is not secret, so it travels as an argument: piping a file through
# `kubectl exec -i` can arrive empty when the remote command exits right away.
kubectl -n "$NS" exec "$POD" -- sh -c 'printf "%s\n" "$1" > /tmp/aggregator.hcl' -- "$POLICY"
bao_root policy write aggregator /tmp/aggregator.hcl >/dev/null

bao_root write auth/kubernetes/role/aggregator \
  bound_service_account_names="$APP_SA" \
  bound_service_account_namespaces="$APP_NS" \
  token_policies=aggregator \
  token_ttl=1h >/dev/null
log "policy and role 'aggregator' in place"

log "done"
