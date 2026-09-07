from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .registry import Conflict, RegistryError, get_registry
from .importer import extract

router = APIRouter(prefix="/api/registry")


class ChannelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="", max_length=160)
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: str = Field(default="", max_length=4096, repr=False)
    scope: str = Field(default="", max_length=80)
    multiplier: float = Field(gt=0, le=1000, allow_inf_nan=False)
    note: str = Field(default="", max_length=2000)
    enabled: bool = True
    status: Literal["recorded", "online"] = "recorded"
    version: int | None = None


class ImportInput(BaseModel):
    text: str = Field(min_length=1, max_length=1_000_000, repr=False)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    channel_id: int = Field(ge=1)
    model: str = Field(min_length=1, max_length=160)
    protocol: Literal["openai", "anthropic"]


def resolve(selection: Selection) -> dict:
    try:
        channel = get_registry().resolve(selection.channel_id)
    except KeyError as exc:
        raise HTTPException(404, "公共渠道不存在") from exc
    except RegistryError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"base_url": channel["base_url"], "api_key": channel["api_key"],
            "model": selection.model, "protocol": selection.protocol}


@router.get("/channels")
async def channels():
    return {"channels": get_registry().list()}


@router.post("/channels")
async def add_channel(body: ChannelInput):
    try:
        return get_registry().save(body.model_dump())
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except RegistryError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/channels/{channel_id}")
async def update_channel(channel_id: int, body: ChannelInput):
    try:
        return get_registry().save(body.model_dump(), channel_id, body.version)
    except KeyError as exc:
        raise HTTPException(404, "公共渠道不存在") from exc
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except RegistryError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/import")
async def import_channels(body: ImportInput):
    try:
        records = extract(body.text)
        if not records:
            raise RegistryError("未识别到包含地址、密钥和倍率的记录，请使用手动新增或检查源文本")
        result = get_registry().import_records(records)
        return {**result, "channels": get_registry().list()}
    except (RegistryError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
