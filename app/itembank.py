"""Quick-admission capability slices and their aggregation weights."""
from typing import Any

from . import test_catalog

PACK_VERSION = test_catalog.CATALOG_VERSION

WEIGHTS = {
    "代码": 0.30,
    "结构化": 0.25,
    "推理": 0.30,
    "指令保持": 0.15,
}


def all_items(model: str = "", seed: int = 1) -> list[tuple[str, dict[str, Any]]]:
    return [(item["dim"], item) for item in test_catalog.admission_items(model, seed)
            if item.get("score_domain") == "ability"]


def _dimension_items(name: str) -> list[dict[str, Any]]:
    return [item for dimension, item in all_items() if dimension == name]


DIMENSIONS: dict[str, dict[str, Any]] = {
    name: {"items": _dimension_items(name), "weight": weight}
    for name, weight in WEIGHTS.items()
}

DIM_ORDER = list(DIMENSIONS)
ABILITY_DIM_ORDER = DIM_ORDER


def estimate_tokens(model: str = "claude") -> int:
    return test_catalog.estimate_tokens(model)
