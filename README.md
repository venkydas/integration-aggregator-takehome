# Integration Aggregator

[![CI](https://github.com/venkydas/integration-aggregator-takehome/actions/workflows/ci.yml/badge.svg)](https://github.com/venkydas/integration-aggregator-takehome/actions/workflows/ci.yml)

This is an internal service that other services call to get a user's current OAuth
access token for GitHub, GitLab, Google or any OIDC provider.

**OpenBao does all the OAuth work.** The
[oauthapp plugin](https://github.com/openbao/openbao-plugin-secrets-oauthapp) handles
code exchange, token storage and refresh. This service only orchestrates the flow.
It implements no OAuth itself and stores no secrets or tokens.

- [DESIGN.md](DESIGN.md): architecture, decisions, multi-replica limits, security
- [PERF.md](PERF.md): load-test results and bottleneck analysis
- [demo/github-demo.txt](demo/github-demo.txt): transcript of the full flow against real GitHub
- [ASSIGNMENT.md](ASSIGNMENT.md): the original problem statement

## Quick start

Requirements: Docker, minikube, kubectl, helm, [uv](https://docs.astral.sh/uv/), make, jq.

```bash
make up      # minikube + OpenBao (+plugin) + mock OIDC provider + the service; idempotent
make smoke   # end-to-end: register, connect, login, callback, async token
make perf    # load test at concurrency 10/50/100 -> perf-report.md
make down    # delete everything
```

`make up` builds both images locally, tagged by a hash of their inputs, and deploys
everything from Helm charts. Running it a second time changes nothing. In CI the same
target deploys the images and chart published to ghcr.io instead.

For local development, `make test` runs the unit tests and `make lint` runs ruff.
`make port-forward` exposes the service on `localhost:8080`.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/providers` | Register or update a provider. The client secret goes only to OpenBao. |
| `POST` | `/providers/{provider}/users/{user}/connect` | Start the OAuth flow. Returns the URL to open in a browser. |
| `GET` | `/callback` | Where the provider redirects. OpenBao exchanges the code. |
| `GET` | `/{provider}/{user}` | Ask for the user's current token. Returns `202` with a `Location` header. |
| `GET` | `/requests/{id}` | Poll for the result: `pending`, `done` (with `access_token`) or `failed`. |
| `GET` | `/healthz`, `/readyz` | Liveness, and readiness (OpenBao reachable and unsealed). |

### Real-provider demo

[`demo/github-demo.txt`](demo/github-demo.txt) is a transcript of all three flows
against real GitHub:

1. Register the provider.
2. Connect a user and approve the app in a browser.
3. Request the token (202, then poll) and receive a `gho_…` token.
4. Call the GitHub API with that token to show it is genuine.

To reproduce it, create a GitHub OAuth App with the callback URL
`http://localhost:8080/callback`. Put its credentials in `.env` (git-ignored) as
`GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET`, then run:

```bash
make up && bash scripts/demo.sh
```

The script never prints the client secret and masks tokens in the transcript.

### Example with real GitHub (by hand)

With the same OAuth App, run `make up` and `make port-forward`, then:

```bash
# 1. Register the provider; the secret is stored in OpenBao and never returned.
curl -X POST localhost:8080/providers -H 'content-type: application/json' \
  -d "{\"name\":\"github\",\"provider\":\"github\",
       \"client_id\":\"$GITHUB_CLIENT_ID\",\"client_secret\":\"$GITHUB_CLIENT_SECRET\"}"

# 2. Connect a user; open the returned URL in a browser and approve.
curl -X POST localhost:8080/providers/github/users/alice/connect
# -> {"authorization_url": "https://github.com/login/oauth/authorize?...", "expires_in": 600}

# 3. Ask for the token: 202 + Location, then poll.
curl -i localhost:8080/github/alice
# -> HTTP/1.1 202 Accepted
#    location: /requests/3q2-x1...
curl localhost:8080/requests/3q2-x1...
# -> {"status": "done", "access_token": "gho_...", "token_type": "Bearer", ...}
```

`provider` can be `github`, `gitlab`, `google`, `oidc` (needs
`provider_options.issuer_url`) or `custom`. Default scopes come from the provider
type and can be overridden with `{"scopes": [...]}` in the connect body.

## Repository layout

```
app/                    FastAPI service: routes, OpenBao client, worker, in-memory stores
tests/                  unit tests (OpenBao mocked with respx)
openbao/Dockerfile      OpenBao image with the oauthapp plugin built from source
charts/aggregator/      Helm chart for the service
deploy/                 OpenBao chart values; mock OIDC provider for the smoke test
scripts/                bootstrap-bao.sh, build-images.sh, smoke.sh, perf.py, demo.sh
demo/                   transcript of the real-GitHub run
.github/workflows/      CI: test -> publish to ghcr.io -> minikube e2e + perf
```

## CI

On every push, [`ci.yml`](.github/workflows/ci.yml):

1. Runs lint and unit tests.
2. Builds and pushes both images and the Helm chart to `ghcr.io/venkydas`.
3. Starts minikube and runs `make up` from the published artifacts.
4. Checks that a second `make up` changes no pods.
5. Runs `make smoke` and `make perf`, and uploads the perf report as the
   `perf-report` artifact.
