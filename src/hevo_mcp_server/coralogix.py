"""Ships the server's log records to Coralogix when CORALOGIX_PRIVATE_KEY is set; otherwise does nothing.

Records are batched on a background thread, so tool calls never wait on Coralogix. If Coralogix
rejects the key, shipping stops and one warning goes to stderr. Records that can't be sent (queue
full, Coralogix unreachable) are dropped rather than slowing the server down.
"""

import json
import logging
import os
import queue
import socket
import sys
import threading
from typing import Any

import httpx2 as httpx

# logging level -> Coralogix severity (1 debug, 3 info, 4 warning, 5 error, 6 critical)
SEVERITY = {logging.DEBUG: 1, logging.INFO: 3, logging.WARNING: 4, logging.ERROR: 5, logging.CRITICAL: 6}
MAX_QUEUE = 10_000
MAX_BATCH = 500
FLUSH_SECONDS = 2.0


def logs_url(domain: str) -> str:
    """`coralogix.com`, `eu2.coralogix.com`, `coralogix.us`, `coralogix.in`, … → its log ingestion URL."""
    return f"https://ingress.{domain.strip().strip('/')}/logs/v1/singles"


class CoralogixHandler(logging.Handler):
    def __init__(
        self,
        private_key: str,
        url: str,
        application: str,
        subsystem: str,
        *,
        transport: httpx.BaseTransport | None = None,
        flush_seconds: float = FLUSH_SECONDS,
    ):
        super().__init__()
        self._key = private_key
        self._url = url
        self._application = application
        self._subsystem = subsystem
        self._host = socket.gethostname()
        self._http = httpx.Client(transport=transport, timeout=10.0)
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(MAX_QUEUE)
        self._flush_seconds = flush_seconds
        self._lock = threading.Lock()  # one batch in flight at a time
        self._stop = threading.Event()
        self.active = True
        self.dropped = 0
        self._warned_network = False
        self._thread = threading.Thread(target=self._run, name="coralogix-shipper", daemon=True)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        if not self.active:
            return
        try:
            entry = {
                "applicationName": self._application,
                "subsystemName": self._subsystem,
                "computerName": self._host,
                "severity": SEVERITY.get(record.levelno, 3),
                "timestamp": record.created * 1000,
                "category": record.name,
                "text": self.format(record),
            }
            self._queue.put_nowait(entry)
        except queue.Full:
            self.dropped += 1
        except Exception:  # noqa: BLE001 - logging handlers must never raise
            self.handleError(record)

    def flush(self) -> None:
        """Sends everything queued so far (blocking)."""
        while self.active and not self._queue.empty():
            self._send_batch()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._flush_seconds + 1)
        self.flush()
        self._http.close()
        super().close()

    def _run(self) -> None:
        while not self._stop.wait(self._flush_seconds):
            self.flush()

    def _send_batch(self) -> None:
        with self._lock:
            batch: list[dict[str, Any]] = []
            while len(batch) < MAX_BATCH:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            if not batch or not self.active:
                return
            try:
                resp = self._http.post(self._url, content=json.dumps(batch),
                                       headers={"Authorization": f"Bearer {self._key}",
                                                "Content-Type": "application/json"})
            except httpx.TransportError as exc:
                self.dropped += len(batch)
                if not self._warned_network:
                    self._warned_network = True
                    _warn(f"cannot reach Coralogix ({type(exc).__name__}); dropping logs until it answers")
                return
            if resp.status_code in (401, 403):
                # Wrong or revoked key: stop for good rather than failing on every batch.
                self.active = False
                self.dropped += len(batch)
                _warn(f"Coralogix rejected the key (HTTP {resp.status_code}); log shipping is off")
            elif resp.status_code >= 400:
                self.dropped += len(batch)
                _warn(f"Coralogix returned HTTP {resp.status_code}; dropped {len(batch)} log records")
            else:
                self._warned_network = False


def _warn(message: str) -> None:
    # Straight to stderr: logging it would feed it back into this handler.
    print(f"hevo-mcp: {message}", file=sys.stderr)


def handler_from_env(formatter: logging.Formatter, default_subsystem: str) -> CoralogixHandler | None:
    """CORALOGIX_PRIVATE_KEY turns shipping on. CORALOGIX_DOMAIN picks the Coralogix region
    (default coralogix.com); CORALOGIX_LOGS_URL overrides the full URL. CORALOGIX_APPLICATION and
    CORALOGIX_SUBSYSTEM name the stream (defaults: hevo-mcp, and the server's mode)."""
    key = os.environ.get("CORALOGIX_PRIVATE_KEY", "").strip()
    if not key:
        return None
    url = os.environ.get("CORALOGIX_LOGS_URL") or logs_url(os.environ.get("CORALOGIX_DOMAIN", "coralogix.com"))
    if not url.startswith("https://"):
        _warn("CORALOGIX_LOGS_URL must use https; log shipping is off")
        return None
    handler = CoralogixHandler(
        key, url,
        application=os.environ.get("CORALOGIX_APPLICATION", "hevo-mcp"),
        subsystem=os.environ.get("CORALOGIX_SUBSYSTEM", default_subsystem),
    )
    handler.setFormatter(formatter)
    return handler
