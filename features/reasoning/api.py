from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal

from shared.api import Selection, resolve
from . import main as engine


class EndpointSelection(Selection):
    thinking_mode: Literal["disabled", "enabled", "adaptive"] = "enabled"
    thinking_budget_tokens: int = Field(default=1024, ge=1024, le=131071)


class TestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: EndpointSelection
    max_tokens: int = Field(default=8192, ge=1, le=131072)
    timeout_seconds: float = Field(default=120, ge=1, le=600)

    @model_validator(mode="after")
    def budget(self):
        if (self.endpoint.protocol == "anthropic" and self.endpoint.thinking_mode == "enabled"
                and self.endpoint.thinking_budget_tokens >= self.max_tokens):
            raise ValueError("手动思考预算必须小于最大输出 token 数")
        return self


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.get("/")(engine.index)
app.get("/api/questions")(engine.get_questions)


@app.post("/api/test")
async def test(body: TestInput):
    endpoint = engine.EndpointConfig(**resolve(body.endpoint), thinking_mode=body.endpoint.thinking_mode,
                                    thinking_budget_tokens=body.endpoint.thinking_budget_tokens)
    report = await engine.run_test(engine.TestRequest(endpoint=endpoint, max_tokens=body.max_tokens,
                                                     timeout_seconds=body.timeout_seconds))
    report["endpoint"]["channel_id"] = body.endpoint.channel_id
    return report
