"""Inspect routing and generation settings without credentials or requests."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import get_args
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.admission import main as engine


def inspect(base_url, model, protocol):
    config = engine.EndpointConfig(base_url=base_url, api_key="synthetic-inspection", model=model, protocol=protocol)
    endpoint = urlsplit(engine.endpoint_url(config))
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("base URL contains credentials, query or fragment")
    settings = [{"question_id": q["id"], "parameters": {key: value for key, value in
                engine.build_payload(config, q).items() if key not in {"instructions", "input", "messages", "system"}}}
                for q in engine.QUESTIONS]
    fingerprint = hashlib.sha256(json.dumps([base_url, config.model, config.protocol, settings], sort_keys=True).encode()).hexdigest()
    return {"model": config.model, "protocol": config.protocol, "requests_sent": 0,
            "masked_host": "host-" + hashlib.sha256(endpoint.hostname.encode()).hexdigest()[:12],
            "settings": settings, "fingerprint": fingerprint, "extracted_at": int(time.time())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://synthetic.example/v1")
    parser.add_argument("--model", default=engine.GPT6_MODEL)
    parser.add_argument("--protocol", choices=get_args(engine.AdmissionProtocol), default="responses")
    args = parser.parse_args()
    try:
        value = inspect(args.base_url, args.model, args.protocol)
    except ValueError:
        parser.error("invalid model or base URL; no requests sent")
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    main()
