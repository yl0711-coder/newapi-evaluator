import random


def text_for_tokens(target, seed):
    rng = random.Random(seed)
    size = target * 4
    heading = "Read the synthetic inventory below. Summarize its patterns as a numbered list.\n"
    rows = [heading]
    length = len(heading)
    index = 1
    while length < size:
        line = f"Record {index}: item {rng.randrange(1000,9999)}; stock {rng.randrange(1,99)}; zone {rng.choice('ABCD')}.\n"
        rows.append(line)
        length += len(line)
        index += 1
    return "".join(rows)[:size]


def request_payload(protocol, model, attempt):
    content = text_for_tokens(attempt["input_tokens"], attempt["seed"])
    payload = {"model": model, "stream": attempt["stream"]}
    if protocol == "responses":
        payload.update(input=content, max_output_tokens=attempt["output_tokens"], store=False)
    else:
        payload["messages"] = [{"role": "user", "content": content}]
        if protocol == "anthropic":
            payload["max_tokens"] = attempt["output_tokens"]
        else:
            payload["max_completion_tokens"] = attempt["output_tokens"]
            if attempt["stream"]:
                payload["stream_options"] = {"include_usage": True}
    return payload
