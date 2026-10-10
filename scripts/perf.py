"""Load test for the async token flow.

Each virtual user loops: GET /{provider}/{user} -> 202 -> poll Location until
the request is done. Two latencies are recorded per request:
  submit: time until the 202 arrives (the cost of the API call itself)
  e2e:    time until the token is available (what a caller actually waits)

Needs a provider with a connected user; `make smoke` creates mock/smoke-user.
"""

import argparse
import asyncio
import json
import math
import os
import platform
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx


class Failure(Exception):
    pass


@dataclass
class LevelResult:
    concurrency: int
    duration_s: float = 0.0
    submit_ms: list[float] = field(default_factory=list)
    e2e_ms: list[float] = field(default_factory=list)
    polls: int = 0
    errors: Counter = field(default_factory=Counter)

    def summary(self) -> dict:
        ok = len(self.e2e_ms)
        total = ok + sum(self.errors.values())
        return {
            "concurrency": self.concurrency,
            "duration_s": round(self.duration_s, 2),
            "requests_ok": ok,
            "errors": dict(self.errors),
            "error_rate": round(1 - ok / total, 4) if total else 0.0,
            "throughput_rps": round(ok / self.duration_s, 1) if self.duration_s else 0.0,
            "polls_per_token": round(self.polls / ok, 1) if ok else 0.0,
            "submit_p50_ms": round(percentile(self.submit_ms, 50), 1),
            "submit_p95_ms": round(percentile(self.submit_ms, 95), 1),
            "e2e_p50_ms": round(percentile(self.e2e_ms, 50), 1),
            "e2e_p95_ms": round(percentile(self.e2e_ms, 95), 1),
        }


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


async def one_request(client: httpx.AsyncClient, path: str) -> tuple[float, float, int]:
    start = time.perf_counter()
    polls = 0
    r = await client.get(path)
    if r.status_code != 202:
        raise Failure(f"submit_http_{r.status_code}")
    submitted = time.perf_counter()

    location = r.headers["location"]
    delay = 0.002  # poll quickly at first, then back off
    while True:
        p = await client.get(location)
        polls += 1
        if p.status_code != 200:
            raise Failure(f"poll_http_{p.status_code}")
        body = p.json()
        if body["status"] == "done":
            break
        if body["status"] == "failed":
            raise Failure(body.get("error_code") or "failed")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 0.05)
    done = time.perf_counter()
    return (submitted - start) * 1000, (done - start) * 1000, polls


async def run_level(
    client: httpx.AsyncClient, path: str, concurrency: int, duration: float
) -> LevelResult:
    result = LevelResult(concurrency)
    deadline = time.perf_counter() + duration

    async def virtual_user() -> None:
        while time.perf_counter() < deadline:
            try:
                submit_ms, e2e_ms, polls = await one_request(client, path)
            except Failure as e:
                result.errors[str(e)] += 1
            except httpx.HTTPError as e:
                result.errors[type(e).__name__] += 1
            else:
                result.submit_ms.append(submit_ms)
                result.e2e_ms.append(e2e_ms)
                result.polls += polls

    start = time.perf_counter()
    await asyncio.gather(*(virtual_user() for _ in range(concurrency)))
    result.duration_s = time.perf_counter() - start
    return result


async def wait_healthy(client: httpx.AsyncClient) -> None:
    while True:
        try:
            if (await client.get("/healthz")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)


def render_markdown(meta: dict, rows: list[dict]) -> str:
    lines = [
        "# Performance report",
        "",
        f"- Date: {meta['date']}",
        f"- Commit: `{meta['commit']}`",
        f"- Target: {meta['target']} (`GET /{meta['provider']}/{meta['user']}`)",
        f"- Duration per level: {meta['duration_s']}s, after a {meta['warmup_s']}s warm-up",
        f"- Load generator: {meta['host']}",
        "",
        "Each virtual user loops: request a token (202), then poll the `Location`"
        " URL until the token is ready. *Submit* is the time to the 202;"
        " *end-to-end* is the time until the token is available.",
        "",
        "| Concurrency | OK | Errors | Throughput (tokens/s) | Polls/token | Submit p50 "
        "| Submit p95 | E2E p50 | E2E p95 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        errors = sum(r["errors"].values())
        lines.append(
            f"| {r['concurrency']} | {r['requests_ok']} | {errors} | {r['throughput_rps']} "
            f"| {r['polls_per_token']} | {r['submit_p50_ms']} ms | {r['submit_p95_ms']} ms "
            f"| {r['e2e_p50_ms']} ms | {r['e2e_p95_ms']} ms |"
        )
    failed = {r["concurrency"]: r["errors"] for r in rows if r["errors"]}
    if failed:
        lines += ["", "Errors by level: " + json.dumps(failed)]
    return "\n".join(lines) + "\n"


async def run(args: argparse.Namespace, levels: list[int]) -> list[dict]:
    path = f"/{args.provider}/{args.user}"
    limits = httpx.Limits(max_connections=max(levels) + 10, max_keepalive_connections=max(levels))

    async with httpx.AsyncClient(base_url=args.base_url, timeout=10, limits=limits) as client:
        try:
            async with asyncio.timeout(30):
                await wait_healthy(client)
        except TimeoutError:
            sys.exit(f"service at {args.base_url} not healthy after 30s")
        try:
            await one_request(client, path)
        except Failure as e:
            hint = (
                " (run `make smoke` first to connect a user)" if "not_connected" in str(e) else ""
            )
            sys.exit(f"pre-flight request failed: {e}{hint}")

        print(f"warm-up {args.warmup}s ...", flush=True)
        await run_level(client, path, min(levels), args.warmup)

        rows = []
        for c in levels:
            print(f"concurrency {c} for {args.duration}s ...", flush=True)
            rows.append((await run_level(client, path, c, args.duration)).summary())
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--provider", default="mock")
    parser.add_argument("--user", default="smoke-user")
    parser.add_argument("--levels", default="10,50,100", help="comma-separated concurrency")
    parser.add_argument("--duration", type=float, default=15, help="seconds per level")
    parser.add_argument("--warmup", type=float, default=3)
    parser.add_argument("--out", default=".", help="directory for perf-report.{md,json}")
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    args = parser.parse_args()
    levels = [int(x) for x in args.levels.split(",")]

    rows = asyncio.run(run(args, levels))

    meta = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "commit": os.environ.get("GITHUB_SHA", "local")[:12],
        "target": args.base_url,
        "provider": args.provider,
        "user": args.user,
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "host": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = render_markdown(meta, rows)
    (out / "perf-report.md").write_text(report)
    (out / "perf-report.json").write_text(json.dumps({"meta": meta, "levels": rows}, indent=2))
    print("\n" + report)

    worst = max(r["error_rate"] for r in rows)
    if worst > args.max_error_rate:
        print(f"error rate {worst:.2%} exceeds {args.max_error_rate:.2%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
