"""Strict JSON text encoding shared by durable, report and monitor stores."""
from __future__ import annotations

import json
from typing import Any


def encode_json(value: Any) -> str:
    """Return the repository's stable, non-NaN JSON representation."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
