"""daemon 与客户端的 unix socket 消息协议（NDJSON：一行一个 JSON 对象）。

M1 仅请求/响应；事件推送留给 M3（tmux 会话通知）。
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

# 错误码（M1 子集，M2 增加 policy_denied）
ERR_METHOD_NOT_FOUND = "method_not_found"
ERR_INVALID_PARAMS = "invalid_params"
ERR_NOT_FOUND = "not_found"
ERR_CONNECTION_FAILED = "connection_failed"
ERR_UNAVAILABLE = "unavailable"
ERR_INTERNAL = "internal"


class RpcRequest(BaseModel):
    """客户端 -> daemon 的单次调用。"""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class RpcError(BaseModel):
    """结构化错误。retry_after 供退避重试（连接池 backoff）。"""

    code: str
    message: str
    retry_after: float | None = None


class RpcOk(BaseModel):
    id: str
    ok: Literal[True] = True
    result: dict[str, Any] = Field(default_factory=dict)


class RpcErr(BaseModel):
    id: str
    ok: Literal[False] = False
    error: RpcError


RpcResponse = RpcOk | RpcErr

_response_adapter: TypeAdapter[RpcResponse] = TypeAdapter(RpcResponse)


def encode_line(model: BaseModel) -> bytes:
    """编码为一行 NDJSON（含换行符）。"""
    return model.model_dump_json().encode("utf-8") + b"\n"


def decode_request(line: bytes | str) -> RpcRequest:
    return RpcRequest.model_validate_json(line)


def decode_response(line: bytes | str) -> RpcResponse:
    return _response_adapter.validate_json(line)
