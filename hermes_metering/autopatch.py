"""Add-only monkey-patches into hermes-agent.

Imported once at process start (via sitecustomize.py). Never raises.

Two integration points:
  1. LLM cost computation — wraps the function that returns a CostResult.
  2. Tool dispatch — wraps the registry's async dispatch to time + count tool calls.

If upstream renames a symbol, the patch silently no-ops. Check edge function
logs (no llm/runtime events arriving) to detect drift.
"""
from __future__ import annotations

import importlib
import logging
import time
from typing import Any

from . import record_llm, record_tool, start_flusher

log = logging.getLogger("hermes_metering.autopatch")


# ---------------------------------------------------------------------------
# 1. LLM patch — wrap usage_pricing.compute_cost (or sibling)
# ---------------------------------------------------------------------------
_LLM_TARGETS = [
    # (module_path, function_name)
    ("agent.usage_pricing", "compute_cost"),
    ("agent.usage_pricing", "calculate_cost"),
    ("hermes.usage_pricing", "compute_cost"),
    ("usage_pricing", "compute_cost"),
]


def _patch_llm() -> bool:
    for mod_path, fn_name in _LLM_TARGETS:
        try:
            mod = importlib.import_module(mod_path)
        except Exception:
            continue
        original = getattr(mod, fn_name, None)
        if not callable(original):
            continue
        if getattr(original, "_hm_patched", False):
            return True

        def make_wrapper(orig):
            def wrapper(*args, **kwargs):
                started = time.perf_counter()
                result = orig(*args, **kwargs)
                latency_ms = int((time.perf_counter() - started) * 1000)
                try:
                    # Best-effort extraction. Function signatures vary; we look
                    # at args[0] (usually `usage`) and args[1] (usually `route`).
                    usage = kwargs.get("usage") or (args[0] if args else None)
                    route = kwargs.get("route") or (args[1] if len(args) > 1 else None)
                    record_llm(
                        model=getattr(route, "model", "") or "",
                        provider=getattr(route, "provider", "") or "",
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "output_tokens",
                                                  getattr(usage, "completion_tokens", 0)) or 0,
                        cached_tokens=getattr(usage, "cache_read_tokens", 0) or 0,
                        latency_ms=latency_ms,
                    )
                except Exception:
                    log.debug("llm record extraction failed", exc_info=True)
                return result
            wrapper._hm_patched = True  # type: ignore[attr-defined]
            return wrapper

        setattr(mod, fn_name, make_wrapper(original))
        log.info("hermes_metering: patched %s.%s", mod_path, fn_name)
        return True
    log.warning("hermes_metering: no LLM cost function found — LLM events disabled")
    return False


# ---------------------------------------------------------------------------
# 2. Tool patch — wrap tools.registry.dispatch
# ---------------------------------------------------------------------------
_TOOL_TARGETS = [
    ("tools.registry", "dispatch"),
    ("agent.tools.registry", "dispatch"),
    ("hermes.tools.registry", "dispatch"),
]

_CATEGORY_MAP = {
    "browser_tool": "browser",
    "browser_camofox": "browser",
    "web_tools": "browser",
    "code_execution_tool": "code",
    "terminal_tool": "code",
    "mcp_tool": "network",
    "memory_tool": "data",
    "file_tools": "data",
}


def _category(name: str) -> str:
    return _CATEGORY_MAP.get(name, "other")


def _patch_tools() -> bool:
    import asyncio
    for mod_path, fn_name in _TOOL_TARGETS:
        try:
            mod = importlib.import_module(mod_path)
        except Exception:
            continue
        original = getattr(mod, fn_name, None)
        if not callable(original):
            continue
        if getattr(original, "_hm_patched", False):
            return True

        is_coro = asyncio.iscoroutinefunction(original)

        if is_coro:
            async def wrapper(tool_name: str, *args, **kwargs):  # type: ignore[misc]
                started = time.perf_counter()
                success = False
                try:
                    result = await original(tool_name, *args, **kwargs)
                    success = True
                    return result
                finally:
                    try:
                        record_tool(
                            tool_name=tool_name,
                            category=_category(tool_name),
                            duration_ms=int((time.perf_counter() - started) * 1000),
                            success=success,
                        )
                    except Exception:
                        log.debug("tool record failed", exc_info=True)
        else:
            def wrapper(tool_name: str, *args, **kwargs):  # type: ignore[misc]
                started = time.perf_counter()
                success = False
                try:
                    result = original(tool_name, *args, **kwargs)
                    success = True
                    return result
                finally:
                    try:
                        record_tool(
                            tool_name=tool_name,
                            category=_category(tool_name),
                            duration_ms=int((time.perf_counter() - started) * 1000),
                            success=success,
                        )
                    except Exception:
                        log.debug("tool record failed", exc_info=True)

        wrapper._hm_patched = True  # type: ignore[attr-defined]
        setattr(mod, fn_name, wrapper)
        log.info("hermes_metering: patched %s.%s (async=%s)", mod_path, fn_name, is_coro)
        return True
    log.warning("hermes_metering: no tool dispatch function found — tool events disabled")
    return False


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
_applied = False


def apply() -> None:
    global _applied
    if _applied:
        return
    _applied = True
    try:
        _patch_llm()
    except Exception:
        log.warning("LLM patch failed", exc_info=True)
    try:
        _patch_tools()
    except Exception:
        log.warning("tool patch failed", exc_info=True)
    try:
        start_flusher()
    except Exception:
        log.warning("flusher start failed", exc_info=True)


# Auto-apply on import.
apply()
