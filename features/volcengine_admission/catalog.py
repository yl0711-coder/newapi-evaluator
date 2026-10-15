"""Official model IDs and explicitly bounded, single-output test specifications.

Contracts/prices checked against Volcengine's public documentation on 2026-10-15.
The estimate is list price, not an account charge or a supplier-enforced cap.
"""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json

OFFICIAL_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
CONTRACT_VERSION = "volcengine-admission-v1-20261015"
VERIFIED_AT = "2026-10-15"
SOURCES = {
    "models": "https://docs.volcengine.com/docs/ark/model-list?lang=zh",
    "prices": "https://console.volcengine.com/ark/region%3Acn-beijing/docs/model-pricing",
    "video": "https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh",
    "image": "https://docs.volcengine.com/docs/ark/image-generation-api?lang=zh",
    "deprecation": "https://docs.volcengine.com/docs/ark/model-deprecation-notice?lang=zh",
}
NOTES = [
    "仅测试本次所选型号，每个型号只提交一次；不会创建持续监控。",
    "估算是官方刊例价，不含免费额度、折扣或资源包，不是扣费硬上限；实际账单未核实。",
    "任务列表鉴权通过仅证明该端点可访问，不证明所有型号已开通或 Key 的来源身份。",
    "停止仅停止后续发送和本地查询；已提交任务可能继续处理和计费。",
    "补充五款为存量兼容型号，已停止新购，计划于 2026-11-24 14:00 UTC+8 下线。",
    "Seedance 2.5/2.0 开通可能要求账户余额超过 200 元或满足官方资源包等条件。",
]


def _video(model_id, name, rate, *, legacy=False, resolutions=None, maximum=15):
    return {"model_id": model_id, "name": name, "kind": "video", "default_selected": not legacy,
            "legacy": legacy, "resolutions": resolutions or ["480p", "720p"], "sizes": [],
            "duration_min": 2 if legacy else 4, "duration_max": 12 if legacy else maximum,
            "supports_frames": legacy, "default_spec": {"resolution": "480p", **({"frames": 29} if legacy else {"duration": 4})},
            "price": {"cny_per_million_tokens": rate,
                      "cny_per_million_tokens_by_resolution": {resolution: (77 if name == "Seedance 2.5" and resolution == "1080p"
                                                                            else 51 if name == "Seedance 2.0" and resolution == "1080p"
                                                                            else 26 if name == "Seedance 2.0" and resolution == "4k" else rate)
                                                               for resolution in (resolutions or ["480p", "720p"])}},
            "eos_at": "2026-11-24T14:00:00+08:00" if legacy else None}


def _image(model_id, name, price, sizes, *, legacy=False):
    return {"model_id": model_id, "name": name, "kind": "image", "default_selected": not legacy,
            "legacy": legacy, "sizes": sizes, "resolutions": [], "supports_frames": False,
            "default_spec": {"size": sizes[0]}, "price": {"cny_per_image": price},
            "eos_at": "2026-11-24T14:00:00+08:00" if legacy else None}


MODELS = [
    _video("doubao-seedance-2-5-260628", "Seedance 2.5", 70, resolutions=["480p", "720p", "1080p"], maximum=30),
    _video("doubao-seedance-2-0-260128", "Seedance 2.0", 46, resolutions=["480p", "720p", "1080p", "4k"]),
    _video("doubao-seedance-2-0-fast-260128", "Seedance 2.0 Fast", 37),
    _video("doubao-seedance-2-0-mini-260615", "Seedance 2.0 Mini", 23),
    _image("doubao-seedream-5-0-pro-260628", "Seedream 5.0 Pro", .30, ["1K", "1.5K", "2K"]),
    _image("doubao-seedream-5-0-flash-260915", "Seedream 5.0 Flash", .12, ["1K", "1.5K", "2K"]),
    _video("doubao-seedance-1-0-pro-250528", "Seedance 1.0 Pro", 15, legacy=True, resolutions=["480p", "720p", "1080p"]),
    _video("doubao-seedance-1-0-pro-fast-251015", "Seedance 1.0 Pro Fast", 4.2, legacy=True, resolutions=["480p", "720p", "1080p"]),
    _image("doubao-seedream-5-0-lite-260128", "Seedream 5.0 Lite", .22, ["2K", "3K", "4K"], legacy=True),
    _image("doubao-seedream-4-5-251128", "Seedream 4.5", .25, ["2K", "4K"], legacy=True),
    _image("doubao-seedream-4-0-250828", "Seedream 4.0（旧版）", .20, ["1K", "2K", "4K"], legacy=True),
]
BY_ID = {row["model_id"]: row for row in MODELS}
# Square output is fixed to keep all estimates reproducible and avoid input-media cost.
VIDEO_SQUARE_PIXELS = {"480p": 640, "720p": 960, "1080p": 1440, "4k": 2880}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def normalize_item(model_id, spec):
    if model_id not in BY_ID:
        raise ValueError("请选择目录中的精确型号")
    model = BY_ID[model_id]
    supplied = {k: v for k, v in spec.items() if v is not None}
    if model["kind"] == "image":
        if set(supplied) - {"size"}:
            raise ValueError("图片型号只接受 size 规格")
        clean = {"size": supplied.get("size", model["default_spec"]["size"])}
        if clean["size"] not in model["sizes"]:
            raise ValueError("此图片型号不支持所选尺寸")
        price = Decimal("0.60") if model_id == "doubao-seedream-5-0-pro-260628" and clean["size"] == "2K" else Decimal(str(model["price"]["cny_per_image"]))
    else:
        if set(supplied) - {"resolution", "duration", "frames"}:
            raise ValueError("视频型号不接受图片 size 参数")
        resolution = supplied.get("resolution", "480p")
        if resolution not in model["resolutions"]:
            raise ValueError("此视频型号不支持所选清晰度")
        clean = {"resolution": resolution, "ratio": "1:1"}
        if "frames" in supplied and "duration" in supplied:
            raise ValueError("frames 与 duration 只能选择一项")
        if "frames" in supplied or not supplied.get("duration") and model["legacy"]:
            frames = supplied.get("frames", 29)
            if not model["supports_frames"] or type(frames) is not int or not 29 <= frames <= 289 or (frames - 25) % 4:
                raise ValueError("此型号帧数必须为 29 至 289 且符合 25+4n")
            clean["frames"] = frames
            frame_count = Decimal(frames)
        else:
            duration = supplied.get("duration", 4)
            if type(duration) is not int or not model["duration_min"] <= duration <= model["duration_max"]:
                raise ValueError("此视频型号不支持所选时长")
            clean["duration"] = duration
            frame_count = Decimal(duration * 24)
        edge = VIDEO_SQUARE_PIXELS[resolution]
        tokens = frame_count * edge * edge / 1024
        price = tokens * Decimal(str(model["price"]["cny_per_million_tokens_by_resolution"][resolution])) / 1_000_000
    result = {"model_id": model_id, "name": model["name"], "kind": model["kind"], "legacy": model["legacy"],
              "spec": clean, "estimated_cny": float(price)}
    if model["kind"] == "video":
        result.update(estimated_output_tokens=float(tokens),
                      unit_price_cny_per_million_tokens=model["price"]["cny_per_million_tokens_by_resolution"][resolution])
    return result


def request_body(item, prompt):
    model = BY_ID[item["model_id"]]
    if model["kind"] == "image":
        body = {"model": model["model_id"], "prompt": prompt, "size": item["spec"]["size"],
                "response_format": "b64_json", "watermark": True}
        if model["legacy"]:
            body["sequential_image_generation"] = "disabled"
        return body
    body = {"model": model["model_id"], "content": [{"type": "text", "text": prompt}], **item["spec"]}
    if not model["legacy"]:
        body["generate_audio"] = False
    return body


def contract_digest(item, prompt_digest):
    """Reproduce a complete request-shape contract without retaining caller text."""
    path = "/images/generations" if item["kind"] == "image" else "/contents/generations/tasks"
    return fingerprint({"contract_version": CONTRACT_VERSION, "method": "POST", "endpoint": OFFICIAL_BASE_URL + path,
                        "body": request_body(item, "sha256:" + prompt_digest)})


def meta():
    return {"catalog": deepcopy(MODELS), "official_base_url": OFFICIAL_BASE_URL, "contract_version": CONTRACT_VERSION,
            "verified_at": VERIFIED_AT, "sources": SOURCES, "notes": NOTES, "default_mode": "mock"}
