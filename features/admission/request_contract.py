"""Credential-free snapshot of the actual request constructors used by a run."""
import hashlib
import json

from . import main as engine


def snapshot(configuration, *, token_enabled):
    endpoints = {side: engine.EndpointConfig(base_url=configuration[side]["base_url"],
        model=configuration[side]["model"], protocol=configuration[side]["protocol"],
        api_key="contract-placeholder") for side in ("candidate", "reference")}
    requests = []
    for number in range(1, configuration["rounds"] + 1):
        for question in engine.QUESTIONS:
            selected = engine.select_question_variant(question, f"{configuration['run_id']}:{number}")
            for side, endpoint in endpoints.items():
                requests.append({"round": number, "question_id": question["id"], "side": side,
                    "variant_id": selected.get("variant_id", "original"),
                    "url": engine.endpoint_url(endpoint), "headers": engine.build_headers(endpoint),
                    "body": engine.build_payload(endpoint, selected)})
    token_requests = []
    if token_enabled:
        from features.token_usage import transport
        for side, endpoint in endpoints.items():
            value = endpoint.model_dump()
            value["protocol"] = transport.protocol(value["protocol"])
            parameters = transport.ASTRA_PARAMETERS if value["model"] == "gpt-6-astra" else transport.PARAMETERS
            token_requests.append({"side": side, "url": transport.endpoint_url(value["base_url"], value["protocol"]),
                "headers": transport.headers(value), "body": transport.payload(value, {"prompt": transport.FIXED_INPUT, **parameters})})
    return {"version": 1, "questions": engine.QUESTIONS, "requests": requests, "token_requests": token_requests}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
