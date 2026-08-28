from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1


class ProtocolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class Request:
    id: str
    method: str
    params: dict[str, Any]


def parse_request(raw: Any) -> Request:
    if not isinstance(raw, dict):
        raise ProtocolError("invalidRequest", "request must be a JSON object")
    request_id = raw.get("id")
    method = raw.get("method")
    params = raw.get("params", {})
    if not isinstance(request_id, str) or not request_id:
        raise ProtocolError("invalidRequest", "request id must be a non-empty string")
    if not isinstance(method, str) or not method:
        raise ProtocolError("invalidRequest", "method must be a non-empty string")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ProtocolError("invalidRequest", "params must be an object when provided")
    return Request(id=request_id, method=method, params=params)


def result_response(request_id: str, result: dict[str, Any] | list[Any] | str | int | float | bool | None) -> dict[str, Any]:
    return {"id": request_id, "result": result}


def error_response(request_id: str | None, error: ProtocolError | Exception) -> dict[str, Any]:
    if isinstance(error, ProtocolError):
        body = error.to_dict()
    else:
        body = {"code": "internalError", "message": str(error) or error.__class__.__name__}
    response: dict[str, Any] = {"error": body}
    if request_id is not None:
        response["id"] = request_id
    return response


def event(type_: str, **params: Any) -> dict[str, Any]:
    return {"method": "event", "params": {"type": type_, **params}}
