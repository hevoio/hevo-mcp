"""Load test for sizing: the real server against a fake Hevo, measured in-process.

    uv run python scripts/load_test.py --clients 50 --seconds 20 --upstream-ms 150

Starts the HTTP server (stateless, bearer auth, same middleware as production) with Hevo replaced by
an in-memory transport that answers a list response after `--upstream-ms` of simulated latency. Then
`--clients` MCP clients each loop `tools/call` as fast as they can for `--seconds`. Prints requests
per second, latency percentiles and the process's peak resident memory, which is what a replica's
CPU and memory requests should be based on. No network beyond localhost, no Hevo credentials.
"""

import argparse
import asyncio
import contextlib
import resource
import socket
import statistics
import sys
import time

import httpx2 as httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from hevo_mcp_server.auth import HevoKeyVerifier, encode_basic
from hevo_mcp_server.client import HevoClient
from hevo_mcp_server.main import http_run_options
from hevo_mcp_server.observability import configure_logging
from hevo_mcp_server.settings import Settings
from hevo_mcp_server.tools import create_server


def fake_hevo(upstream_ms: float, rows: int):
    page = {"data": [{"id": i, "name": f"pipeline-{i}", "status": "ACTIVE"} for i in range(rows)],
            "has_more": False}

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(upstream_ms / 1000)
        return httpx.Response(200, json=page)

    return httpx.MockTransport(handler)


def rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024 if sys.platform == "darwin" else 1024)


async def worker(url: str, token: str, seconds: float, latencies: list[float], errors: list[str]) -> None:
    transport = StreamableHttpTransport(url, auth=token, headers={"X-Hevo-Region": "us"})
    async with Client(transport) as c:
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            start = time.perf_counter()
            try:
                out = (await c.call_tool("pipelines_read", {"name": "list_pipelines"},
                                         raise_on_error=False)).structured_content or {}
                if "error" in out:
                    errors.append(out["error"])
            except Exception as exc:  # noqa: BLE001 - counted, not raised
                errors.append(type(exc).__name__)
            latencies.append((time.perf_counter() - start) * 1000)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--clients", type=int, default=50)
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--upstream-ms", type=float, default=150, help="simulated Hevo latency")
    parser.add_argument("--rows", type=int, default=100, help="rows in the fake list response")
    args = parser.parse_args()

    configure_logging("http")
    import logging
    logging.getLogger("hevo_mcp").setLevel("ERROR")  # the log volume would dominate the measurement
    settings = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0)
    server = create_server(remote=True, auth=HevoKeyVerifier(), settings=settings,
                           client=HevoClient(fake_hevo(args.upstream_ms, args.rows), settings=settings))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    task = asyncio.create_task(server.run_http_async(
        host="127.0.0.1", port=port, show_banner=False, **http_run_options(settings)))
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(50):
        try:
            async with httpx.AsyncClient() as h:
                await h.get(url)
            break
        except httpx.TransportError:
            await asyncio.sleep(0.1)

    baseline = rss_mb()
    token = encode_basic("key-load-test", "secret-load-test")
    latencies: list[float] = []
    errors: list[str] = []
    started = time.perf_counter()
    try:
        await asyncio.gather(*(worker(url, token, args.seconds, latencies, errors) for _ in range(args.clients)))
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    elapsed = time.perf_counter() - started
    latencies.sort()
    pct = lambda p: latencies[min(len(latencies) - 1, int(len(latencies) * p))]

    print(f"clients={args.clients} seconds={args.seconds} upstream_ms={args.upstream_ms} rows={args.rows}")
    print(f"requests={len(latencies)} rps={len(latencies) / elapsed:.0f} errors={len(errors)}")
    if latencies:
        print(f"latency_ms p50={pct(0.5):.0f} p95={pct(0.95):.0f} p99={pct(0.99):.0f} "
              f"mean={statistics.fmean(latencies):.0f} (upstream {args.upstream_ms} ms of that)")
    print(f"peak_rss_mb={rss_mb():.0f} (before load {baseline:.0f}); includes the {args.clients} clients")
    if errors:
        print("errors:", {e: errors.count(e) for e in set(errors)})
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
