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
    started_at: float | None = None
    inflight_started_at: float | None = None
    first_content_at: float | None = None
    ended_at: float | None = None
    finish_reason: str = ''
    burst_label: str = ''
    workload_profile: str = 'short'
    output_limit: int | None = None
    headers_at: float | None = None
    last_output_at: float | None = None
    max_output_gap_ms: float = 0
    piece_count: int = 0
    upstream_error_kind: str = ''

    def public(self):
        return asdict(self)
