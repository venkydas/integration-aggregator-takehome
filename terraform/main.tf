# OpenBao configuration for the aggregator. Initialising and unsealing happen
# before this (scripts/bootstrap-bao.sh): Terraform needs a working, unsealed
# OpenBao and a token before it can manage anything.

# --- The OAuth secrets engine ---------------------------------------------------

resource "vault_plugin" "oauthapp" {
  type    = "secret"
  name    = "oauthapp"
  command = "oauthapp"
  sha256  = var.plugin_sha256
}

resource "vault_mount" "oauth2" {
  path        = "oauth2"
  type        = vault_plugin.oauthapp.name
  description = "OAuth 2.0 client secrets and user tokens (oauthapp plugin)"
}

# --- How the aggregator authenticates ---------------------------------------------

# Pods log in with their service-account token; OpenBao verifies it with the
# Kubernetes TokenReview API using its own service account.
resource "vault_auth_backend" "kubernetes" {
  type = "kubernetes"
}

resource "vault_kubernetes_auth_backend_config" "this" {
  backend         = vault_auth_backend.kubernetes.path
  kubernetes_host = "https://kubernetes.default.svc"
}

# Least privilege: manage providers and credentials under oauth2/, nothing else.
resource "vault_policy" "aggregator" {
  name   = "aggregator"
  policy = file("${path.module}/policies/aggregator.hcl")
}

resource "vault_kubernetes_auth_backend_role" "aggregator" {
  backend                          = vault_auth_backend.kubernetes.path
  role_name                        = "aggregator"
  bound_service_account_names      = [var.app_service_account]
  bound_service_account_namespaces = [var.app_namespace]
  token_policies                   = [vault_policy.aggregator.name]
  token_ttl                        = 3600
}
