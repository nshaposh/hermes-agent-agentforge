"""hermes_metering — add-only usage metering for hermes-agent.

Public API:
    record_llm(model, provider, prompt_tokens, completion_tokens, cached_tokens=0, latency_ms=0)
    record_tool(tool_name, category, duration_ms, success)
    start_flusher()  # idempotent

Configuration via environment variables (set by the platform at deploy time):
    HERMES_METERING_INGEST_URL   Required. Supabase edge function URL for ingest-usage.
    HERMES_METERING_TOKEN        Required. Bearer token (per-agent JWT or shared secret).
    AGENT_ID                     Required. Agent UUID — included in every event.
    HERMES_METERING_FLUSH_SEC    Optional. Default 15.
    HERMES_METERING_BATCH_MAX    Optional. Default 100.

If env vars are missing, all record_* calls become no-ops. Never raises.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

from ._flusher import Flusher, Event

log = logging.getLogger("hermes_metering")

_flusher: Flusher | None = None


def _get_flusher() -> Flusher | None:
    global _flusher
    if _flusher is not None:
        return _flusher
    url = os.environ.get("HERMES_METERING_INGEST_URL")
    token = os.environ.get("HERMES_METERING_TOKEN")
    agent_id = os.environ.get("AGENT_ID")
    if not (url and token and agent_id):
        return None
    flush_sec = float(os.environ.get("HERMES_METERING_FLUSH_SEC", "15"))
    batch_max = int(os.environ.get("HERMES_METERING_BATCH_MAX", "100"))
    _flusher = Flusher(
        url=url,
        token=token,
        agent_id=agent_id,
        flush_interval_sec=flush_sec,
        batch_max=batch_max,
    )
    return _flusher


def start_flusher() -> None:
    """Start the background flusher. Safe to call multiple times."""
    f = _get_flusher()
    if f is None:
        log.info("hermes_metering disabled — missing env vars")
        return
    f.start()


def _emit(kind: str, quantity: float, unit: str, metadata: dict[str, Any]) -> None:
    f = _get_flusher()
    if f is None:
        return
    try:
        f.enqueue(Event(
            event_id=str(uuid.uuid4()),
            kind=kind,
            quantity=quantity,
            unit=unit,
            metadata=metadata,
            ts=time.time(),
        ))
    except Exception:  # never break the host
        log.debug("enqueue failed", exc_info=True)


def record_llm(
    model: str,
    provider: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    latency_ms: int = 0,
) -> None:
    try:
        total = int(prompt_tokens or 0) + int(completion_tokens or 0)
        _emit(
            kind="llm",
            quantity=total,
            unit="tokens",
            metadata={
                "model": model or "",
                "provider": provider or "",
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "cached_tokens": int(cached_tokens or 0),
                "latency_ms": int(latency_ms or 0),
            },
        )
    except Exception:
        log.debug("record_llm failed", exc_info=True)


def record_tool(
    tool_name: str,
    category: str,
    duration_ms: int,
    success: bool,
) -> None:
    try:
        _emit(
            kind="runtime",
            quantity=int(duration_ms or 0),
            unit="ms_tool",
            metadata={
                "tool_name": tool_name or "",
                "category": category or "other",
                "success": bool(success),
            },
        )
    except Exception:
        log.debug("record_tool failed", exc_info=True)


__all__ = ["record_llm", "record_tool", "start_flusher"]
