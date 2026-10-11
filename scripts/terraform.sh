#!/usr/bin/env bash
# Run Terraform against OpenBao in the cluster.
#   terraform.sh apply   configure OpenBao (idempotent)
#   terraform.sh plan    exit 0 if OpenBao matches the code, 2 if it drifted
#
# Reaches OpenBao through a port-forward. The root token is read from the
# cluster Secret straight into VAULT_TOKEN: never printed, never in argv.
set -euo pipefail

ACTION="${1:-apply}"
NS="${BAO_NAMESPACE:-openbao}"
PORT="${BAO_LOCAL_PORT:-18200}"
TF="terraform -chdir=terraform"

kubectl -n "$NS" port-forward svc/openbao "$PORT:8200" >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/v1/sys/health" && break
  sleep 1
done

export VAULT_ADDR="http://127.0.0.1:$PORT"
VAULT_TOKEN=$(kubectl -n "$NS" get secret openbao-init -o jsonpath='{.data.root_token}' | base64 -d)
export VAULT_TOKEN
export KUBE_CONFIG_PATH="${KUBE_CONFIG_PATH:-$HOME/.kube/config}"
TF_VAR_plugin_sha256=$(kubectl -n "$NS" exec openbao-0 -- cat /openbao/oauthapp.sha256)
export TF_VAR_plugin_sha256
export TF_IN_AUTOMATION=1

$TF init -input=false -no-color >/dev/null

case "$ACTION" in
  apply)
    $TF apply -input=false -auto-approve -no-color -compact-warnings
    # A follow-up plan must be empty: proves the apply fully converged.
    $TF plan -input=false -detailed-exitcode -no-color >/dev/null
    echo "[terraform] OpenBao matches terraform/"
    ;;
  plan)
    set +e
    $TF plan -input=false -detailed-exitcode -no-color
    rc=$?
    set -e
    case $rc in
      0) echo "[terraform] no drift" ;;
      2) echo "[terraform] DRIFT: OpenBao differs from terraform/" ;;
    esac
    exit $rc
    ;;
  *)
    echo "usage: $0 apply|plan" >&2
    exit 64
    ;;
esac
