"""Auto-loaded by Python at interpreter startup if on PYTHONPATH.

Keep this file at the repo root. The Dockerfile / Railway env must set
PYTHONPATH so that this directory is importable, e.g.:

    ENV PYTHONPATH="/opt/hermes:${PYTHONPATH}"

This is the ONLY integration point with upstream hermes-agent — no source
file needs to be modified.
"""
try:
    import hermes_metering.autopatch  # noqa: F401  (side-effect: applies patches)
except Exception:
    # Never break the host process. Missing env vars → silent disable.
    import logging
    logging.getLogger("hermes_metering").warning(
        "hermes_metering failed to load", exc_info=True
    )
