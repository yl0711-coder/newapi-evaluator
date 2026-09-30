"""Small deterministic runner boundary for Eval workflow tasks."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .workflow import WorkflowQueue


def run_one(queue: WorkflowQueue, handler: Callable[[dict[str, Any], Callable[[], bool]], dict[str, Any]]) -> dict[str, Any] | None:
    task = queue.claim_next()
    if task is None:
        return None
    if queue.request_cancelled(task["id"]):
        return queue.finish(task["id"], "cancelled", {"reason": "cancel_requested"})
    started = time.monotonic()
    try:
        result = handler(task["payload"]["payload"], lambda: queue.request_cancelled(task["id"]))
        if time.monotonic() - started > task["budget_seconds"]:
            return queue.finish(task["id"], "failed", {"elapsed_seconds": time.monotonic() - started}, "任务超过预算")
        if queue.request_cancelled(task["id"]):
            return queue.finish(task["id"], "cancelled", {"reason": "cancel_requested"})
        return queue.finish(task["id"], "succeeded", result)
    except Exception as exc:  # handler failures become auditable task state
        return queue.finish(task["id"], "failed", {}, str(exc))
