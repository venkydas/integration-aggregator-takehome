terraform {
  required_version = ">= 1.6"

  required_providers {
    # OpenBao is API-compatible with Vault, so the Vault provider manages it.
    vault = {
      source  = "hashicorp/vault"
      version = "~> 5.12"
    }
  }

  # State is kept in a Secret inside the cluster it describes, so it is created
  # and destroyed together with that cluster (`make down`) and can never go
  # stale. The kubeconfig comes from KUBE_CONFIG_PATH.
  backend "kubernetes" {
    secret_suffix = "openbao-config"
    namespace     = "openbao"
  }
}

provider "vault" {
  # VAULT_ADDR and VAULT_TOKEN come from the environment (scripts/terraform.sh),
  # so the token is never written to a file or passed on a command line.
  skip_child_token = true
}
