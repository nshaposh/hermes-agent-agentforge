"""Background batch flusher. Stdlib only (urllib + threading) — no extra deps."""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib import request as urlrequest
from urllib.error import URLError

log = logging.getLogger("hermes_metering.flusher")


@dataclass
class Event:
    event_id: str
    kind: str
    quantity: float
    unit: str
    metadata: dict[str, Any]
    ts: float = field(default_factory=time.time)

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "quantity": self.quantity,
            "unit": self.unit,
            "metadata": self.metadata,
            "ts": self.ts,
        }


class Flusher:
    def __init__(
        self,
        url: str,
        token: str,
        agent_id: str,
        flush_interval_sec: float = 15.0,
        batch_max: int = 100,
        queue_max: int = 10_000,
    ):
        self.url = url
        self.token = token
        self.agent_id = agent_id
        self.flush_interval_sec = flush_interval_sec
        self.batch_max = batch_max
        self._q: queue.Queue[Event] = queue.Queue(maxsize=queue_max)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = threading.Lock()

    def start(self) -> None:
        with self._started:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="hermes-metering-flusher", daemon=True
            )
            self._thread.start()
            log.info("hermes_metering flusher started (interval=%ss, batch=%s)",
                     self.flush_interval_sec, self.batch_max)

    def stop(self) -> None:
        self._stop.set()

    def enqueue(self, event: Event) -> None:
        try:
            self._q.put_nowait(event)
        except queue.Full:
            log.warning("hermes_metering queue full — dropping event")

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.flush_interval_sec)
            self._drain_once()
        # final drain on shutdown
        self._drain_once()

    def _drain_once(self) -> None:
        batch: list[Event] = []
        while len(batch) < self.batch_max:
            try:
                batch.append(self._q.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return
        try:
            self._post(batch)
        except Exception:
            log.warning("hermes_metering flush failed; dropping %d events", len(batch),
                        exc_info=True)

    def _post(self, batch: list[Event]) -> None:
        body = json.dumps({
            "batch_id": str(uuid.uuid4()),
            "agent_id": self.agent_id,
            "events": [e.to_payload() for e in batch],
        }).encode("utf-8")
        req = urlrequest.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urlrequest.urlopen(req, timeout=10) as resp:
                if resp.status >= 300:
                    log.warning("ingest returned %s", resp.status)
        except URLError as e:
            log.warning("ingest network error: %s", e)
