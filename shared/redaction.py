import json


def scrub(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]").replace(json.dumps(secret)[1:-1], "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {scrub(k, secrets): scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, secrets) for v in value]
    return value


class EventRedactor:
    def __init__(self, secrets):
        self.secrets = sorted({variant for secret in secrets if secret
                               for variant in (secret, json.dumps(secret)[1:-1])}, key=len, reverse=True)
        self.keep = max(map(len, self.secrets), default=1) - 1
        self.pending = {}

    def filter(self, event):
        if event["type"] == "chunk":
            output = {**event}
            for field in ("content", "reasoning", "raw_response"):
                key = (event["question_id"], event["side"], field)
                buffer = self.pending.get(key, "") + event.get(field, "")
                cutoff = max(0, len(buffer) - self.keep)
                for secret in self.secrets:
                    index = buffer.find(secret)
                    while index >= 0:
                        if index < cutoff < index + len(secret):
                            cutoff = index
                        index = buffer.find(secret, index + 1)
                output[field] = scrub(buffer[:cutoff], self.secrets)
                self.pending[key] = buffer[cutoff:]
            return [output]
        output = []
        if event["type"] == "side_finished":
            tail = {"type": "chunk", "question_id": event["question_id"], "side": event["side"]}
            for field in ("content", "reasoning", "raw_response"):
                tail[field] = scrub(self.pending.pop((event["question_id"], event["side"], field), ""), self.secrets)
            if any(tail.get(field) for field in ("content", "reasoning", "raw_response")):
                output.append(tail)
        safe = {**event}
        for field in ("error", "finish_reason", "response_format"):
            if field in safe:
                safe[field] = scrub(safe[field], self.secrets)
        output.append(safe)
        return output
