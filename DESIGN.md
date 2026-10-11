# Design

## Shape

```
caller ── POST /providers ─────────────► aggregator ── PUT  oauth2/servers/<p> ───────► OpenBao
caller ── POST …/users/<u>/connect ────► aggregator ── PUT  oauth2/auth-code-url ─────►   + oauthapp
browser ─ provider login ─► GET /callback?code&state ─ PUT  oauth2/creds/<p>/<u> ────►   (exchange,
caller ── GET /<p>/<u> → 202 Location ─► worker ────── GET  oauth2/creds/<p>/<u> ────►    store,
caller ── GET /requests/<id> → token                                                       refresh)
```

The work is split three ways:

- **OpenBao and the plugin** own every secret: client secrets, refresh tokens and
  access tokens. Client secrets are write-only and are never returned on read. The
  plugin refreshes a token whenever a read finds it close to expiry.
- **The aggregator** validates input, tracks OAuth `state` and runs the async request
  queue. It holds no secret at rest. Even its own OpenBao credential is short-lived:
  the pod logs in with its Kubernetes service-account token (Kubernetes auth), and
  logs in again on expiry or a 403.
- **The infrastructure** is `make up`, which is idempotent:
  - minikube
  - the official OpenBao chart with a custom image, because the plugin has no
    release binaries and is built from a pinned commit
  - `bootstrap-bao.sh`, which initialises and unseals OpenBao (checking state first)
  - Terraform (`terraform/`), which declares the rest of OpenBao's configuration
  - the aggregator chart

## Decisions

- **State and CSRF.** `/connect` generates a 256-bit random `state` and maps it to
  `(provider, user)` for 10 minutes. `/callback` deletes the entry as it reads it,
  so a state value works once and replays fail.
- **Async tokens.** `GET /<p>/<u>` only enqueues a request and returns `202` with a
  `Location` header. A fixed pool of asyncio workers reads the token from OpenBao and
  records `done` or `failed`. Results expire after 5 minutes. When the bounded queue
  is full, the service returns `503` with `Retry-After` instead of letting memory grow.
  Unexpected errors mark the request `failed`, so it is never left `pending`.
- **Safe names.** Provider and user names become part of OpenBao paths
  (`creds/<p>/<u>`). They are validated against strict patterns, so `/`, `..` and the
  names of our own routes cannot be used.
- **Least privilege.** The `aggregator` policy allows only `oauth2/servers/*`,
  `oauth2/auth-code-url` and `oauth2/creds/*`. A token issued under it gets 403 on
  `sys/` (verified).
- **Reproducible deploys.** Local images are tagged by a hash of their inputs, so
  unchanged code means no build, no image load and no rollout. CI verifies that a
  second `make up` leaves every pod untouched.
- **OpenBao configuration as code.** Terraform declares the plugin, the `oauth2/`
  mount, Kubernetes auth, the policy and the role. Initialise and unseal stay in a
  script, because Terraform needs an unsealed OpenBao and a token before it can do
  anything.
  - **State** lives in a Secret inside the cluster (the `kubernetes` backend), so it
    is created and destroyed with the cluster and never goes stale.
  - **Drift.** `make drift` (`terraform plan -detailed-exitcode`) catches manual
    changes, and CI requires it to be clean. A hand-widened policy is reported and
    then reverted by `make up` (tested).
  - **Known gap.** Upgrading the plugin binary needs a `plugin reload`, which
    Terraform does not perform.

## Running more than one replica

The deployment is pinned to 1 replica with the `Recreate` strategy, because three
things live in process memory:

| In memory | What breaks with N replicas |
|---|---|
| OAuth `state` map | The provider redirects the browser to `/callback`, which can land on a pod that never issued that state. The user gets "unknown or expired state" and connecting fails at random. |
| Request results | `GET /requests/<id>` can hit a pod that never saw the request, so the poll returns `404` even though another pod has the token ready. |
| Work queue | Queued requests are lost when their pod restarts or is rescheduled. |

Sticky sessions would only paper over this. The browser's callback and the caller's
polls are separate clients, and a pod restart still loses everything.

**Fix: move these three things into shared, expiring storage.** Redis fits well:

- **State:** `SET state:<s> <p,u> EX 600`, and `GETDEL` in `/callback`. It stays
  single-use, now across all pods.
- **Results:** `SET req:<id> <json> EX 300`. Any pod can answer a poll.
- **Queue:** a Redis Stream with a consumer group, so any pod's workers can take
  work and unacknowledged entries are retried.

This still keeps tokens out of the service in any lasting sense. A result holds a
token only for its 5-minute lifetime, as it does in memory today. Postgres would also
work; Redis wins because of native TTLs. To scale this properly, OpenBao should also
run in HA mode (integrated Raft storage, 3 nodes), because it is the real hot path
(see PERF.md).

## Security notes and what production would change

- No secrets in the repo. `.gitignore` covers env files, unseal keys, root tokens
  and tfstate. Client secrets travel only from the API to OpenBao: they are a
  `SecretStr` in the service, are never logged, and are never echoed back (tested).
- The access log records paths only. The default uvicorn log would have written
  `/callback?code=…&state=…`.
- The service container runs as non-root uid 10001 with a read-only root filesystem,
  all capabilities dropped and the RuntimeDefault seccomp profile.
- **Local shortcuts:**
  - The unseal key and root token live in a Kubernetes Secret. Production would use
    auto-unseal (cloud KMS) and revoke the root token after bootstrap.
  - TLS is off inside the cluster. Production would enable OpenBao TLS and add
    NetworkPolicies, so only the aggregator can reach OpenBao.
- **Caller authentication.** The API trusts whoever can reach it, as an internal
  service. Anyone holding a request id (128-bit, short-lived) can read that token.
  Production should authenticate callers (mTLS or service tokens) and authorize
  `(caller, provider, user)`.
