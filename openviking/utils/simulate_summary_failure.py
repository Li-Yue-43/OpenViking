# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Runtime toggle for simulating summary-generation failures.

This lives in its own module to avoid circular imports between
``semantic_processor`` and ``media.utils``.
"""

import os
from pathlib import Path
from typing import Optional


def should_simulate_summary_failure() -> Optional[str]:
    """Check whether to simulate a summary generation failure.

    Two ways to enable:

    1. Set environment variable ``OV_SIMULATE_SUMMARY_FAILURE`` to a truthy
       value (``1``, ``true``, ``yes``, ``on``).
    2. Create a flag file whose path is given by ``OV_SIMULATE_SUMMARY_FAILURE_FILE``
       (or the default ``~/.openviking/simulate_summary_failure.flag``) and write
       ``1``, ``true``, ``yes`` or ``on`` as its first line.

    The flag file is checked on every call, so it can be toggled at runtime
    without restarting the service. The environment variable is checked first.

    Returns the simulated error message if enabled, otherwise None.
    """
    env_value = os.environ.get("OV_SIMULATE_SUMMARY_FAILURE", "").strip()
    if env_value.lower() in ("1", "true", "yes", "on"):
        return os.environ.get(
            "OV_SIMULATE_SUMMARY_FAILURE_MESSAGE",
            "Simulated summary failure: context window exceeded",
        )

    flag_path = os.environ.get(
        "OV_SIMULATE_SUMMARY_FAILURE_FILE",
        str(Path.home() / ".openviking" / "simulate_summary_failure.flag"),
    )
    try:
        if not Path(flag_path).exists():
            return None
        content = Path(flag_path).read_text(encoding="utf-8").strip()
        first_line = content.splitlines()[0].strip().lower() if content else ""
        if first_line in ("1", "true", "yes", "on"):
            return os.environ.get(
                "OV_SIMULATE_SUMMARY_FAILURE_MESSAGE",
                "Simulated summary failure: context window exceeded",
            )
    except Exception:
        pass
    return None
