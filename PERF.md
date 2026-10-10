# Performance

`make perf` (script: [`scripts/perf.py`](scripts/perf.py)) load-tests the async token
flow. Each virtual user loops: `GET /{provider}/{user}` → `202` → poll the `Location`
URL until the token is ready. It reports p50/p95 for *submit* (time to the 202) and
*end-to-end* (time until the token is available), plus throughput, at concurrency
10, 50 and 100. CI runs it on every push and uploads `perf-report.md`/`.json` as
an artifact.

## Local run

minikube (docker driver, **2 CPUs**) on a 16-CPU laptop under WSL2; load sent through
`kubectl port-forward`; mock OIDC provider; 15 s per level after a 3 s warm-up.

| Concurrency | OK | Errors | Tokens/s | Polls/token | Submit p50 | Submit p95 | E2E p50 | E2E p95 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 10 | 1755 | 0 | 116.5 | 1.2 | 37.5 ms | 65.4 ms | 82.1 ms | 150.2 ms |
| 50 | 1132 | 0 | 73.6 | 1.0 | 243.1 ms | 993.7 ms | 573.8 ms | 1541.9 ms |
| 100 | 1148 | 0 | 73.1 | 1.0 | 476.0 ms | 2052.6 ms | 1060.1 ms | 3364.5 ms |

Zero errors at every level, so back-pressure holds: requests queue and slow down
rather than fail. Throughput varied between about 75 and 170 tokens/s across runs
on the same machine. Treat these numbers as relative, not as capacity figures.

## Where the time goes

Throughput stops growing beyond concurrency 10, and latency grows roughly with
concurrency. That is the classic shape of a saturated resource. Two plausible
suspects turned out to be wrong when measured:

- **Not the port-forward.** Running the same test from a pod inside the cluster was
  no faster.
- **Not polling overhead.** Polls per token were about 1. The worker usually
  finishes before the first poll, so each token costs about two HTTP requests.

Taking a CPU snapshot on the node during a run at concurrency 50 found the cause:

| Process | CPU (% of one core) |
|---|---:|
| `bao` (OpenBao) | 48 |
| `python` (aggregator) | 45 |
| `oauthapp` (plugin) | 32 |
| containerd, kube-apiserver, kubelet, etcd | ~47 |

Together this is about 180% of the node's 2-CPU allowance, so **the node is
CPU-bound**. The largest consumer is OpenBao together with the plugin (~80%): every
token request is a `GET oauth2/creds/...`, which crosses the plugin's gRPC boundary
and reads storage. The aggregator itself uses under half a core.

## What would raise the ceiling

In order of payoff:

1. **More CPU for OpenBao.** Give the node or the OpenBao pod more cores. OpenBao
   is the hot path by design, because it is the only place tokens live.
2. **Coalesce identical in-flight requests.** Concurrent requests for the same
   `(provider, user)` could share one OpenBao read ("single-flight"). This stores
   nothing beyond the life of the request, so the no-persisted-tokens rule still
   holds. Short-TTL token caching would help more, but it means holding tokens in
   our process, which this design deliberately avoids.
3. **Run more aggregator replicas.** This is only useful once OpenBao has headroom,
   and it needs shared request/state storage first (see DESIGN.md).
