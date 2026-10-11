# Policy for the aggregator service: register providers, start the OAuth flow,
# exchange codes and read tokens. No access to anything outside oauth2/.

path "oauth2/servers/*" {
  capabilities = ["create", "update", "read"]
}

path "oauth2/auth-code-url" {
  capabilities = ["update"]
}

path "oauth2/creds/*" {
  capabilities = ["create", "update", "read"]
}
