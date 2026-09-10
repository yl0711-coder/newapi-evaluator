from dataclasses import asdict, dataclass


@dataclass
class Result:
    request_id: str
    stage: str
    concurrency: int
    phase: str = 'load'
    status: int = 0
    error: str = ''
    success: bool = False
    complete: bool = False
    latency_ms: float = 0
    ttft_ms: float | None = None
    queue_ms: float = 0
    output_units: int = 0
    output_units_per_second: float = 0
    output_sha256: str = ''
    account: int | None = None
    collapse: bool = False
    attempt: int = 1
    step: int | None = None

    def public(self):
        return asdict(self)
