"""协议层测试：NDJSON 编解码与模型校验。"""

from pydantic import ValidationError

from clu.protocol import (
    RpcErr,
    RpcError,
    RpcOk,
    RpcRequest,
    decode_request,
    decode_response,
    encode_line,
)


def test_request_roundtrip() -> None:
    req = RpcRequest(method="ping", params={"a": 1})
    line = encode_line(req)
    assert line.endswith(b"\n")
    assert decode_request(line) == req


def test_decode_ok_response() -> None:
    resp = decode_response('{"id": "x", "ok": true, "result": {"pong": true}}')
    assert isinstance(resp, RpcOk)
    assert resp.result == {"pong": True}


def test_decode_err_response_with_retry_after() -> None:
    resp = decode_response(
        '{"id": "x", "ok": false, "error": {"code": "connection_failed",'
        ' "message": "backoff", "retry_after": 1.5}}'
    )
    assert isinstance(resp, RpcErr)
    assert resp.error.retry_after == 1.5
    assert resp.error.code == "connection_failed"


def test_decode_rejects_missing_id() -> None:
    import pytest

    with pytest.raises(ValidationError):
        decode_response('{"ok": true, "result": {}}')


def test_rpc_error_model_defaults() -> None:
    err = RpcError(code="internal", message="boom")
    assert err.retry_after is None
