#!/usr/bin/env bash
# Initialise and unseal OpenBao running in the cluster. Everything after that
# (plugin, mount, auth, policy) is declared in terraform/ and applied by
# scripts/terraform.sh: Terraform needs an unsealed OpenBao and a token first.
# Safe to run any number of times: every step checks current state first.
#
# Unseal key + root token are kept in a Kubernetes Secret (never on disk, never
# printed). Fine for a local cluster; production would use auto-unseal (KMS).
set -euo pipefail

NS="${BAO_NAMESPACE:-openbao}"
POD="${BAO_POD:-openbao-0}"
INIT_SECRET="openbao-init"

log() { printf '[bootstrap] %s\n' "$*"; }

# Run the bao CLI inside the pod without a token.
bao_anon() { kubectl -n "$NS" exec "$POD" -- bao "$@"; }

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

log "done"
