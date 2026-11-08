"""A minimal MCP server over stdio, standard library only.

The official SDK costs ~60 MB of memory and ~0.4 s at start because importing it loads the whole
client stack too. An MCP client starts one server per session, so sozai speaks the small part of
the protocol it needs itself: tools only, both protocol eras.

- Handshake era (2024-11-05 ... 2025-11-25): `initialize`, then requests on that session.
- Modern era (2026-07-28): stateless; `server/discover`, and every request carries its protocol
  version in `_meta`. Results carry `resultType` and a `serverInfo` stamp in `_meta`.

Compatibility is checked against the official SDK client in tests/test_stdio.py.
"""
from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

HANDSHAKE_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MODERN_VERSIONS = ("2026-07-28",)
PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
UNSUPPORTED_PROTOCOL_VERSION = -32022
CACHEABLE_METHODS = {"server/discover", "tools/list"}


class ToolError(Exception):
    """A failure the agent should read (returned as a tool result with isError)."""


@dataclass
class ToolContent:
    """A tool result given as MCP content blocks (text and images), for tools that return pictures."""
    blocks: list[dict]
    structured: dict | None = None


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Any]
    annotations: dict = field(default_factory=dict)

    def describe(self) -> dict:
        out = {"name": self.name, "description": self.description, "inputSchema": self.input_schema}
        if self.annotations:
            out["annotations"] = self.annotations
        return out


# --- argument checking (the subset of JSON Schema the tools use) ---

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict, "null": type(None)}


def _check(value: Any, schema: dict, path: str) -> Any:
    if "anyOf" in schema:
        errors = []
        for option in schema["anyOf"]:
            try:
                return _check(value, option, path)
            except ToolError as error:
                errors.append(str(error))
        raise ToolError(errors[-1] if len(errors) == 1 else f"{path}: {value!r} matches none of the allowed forms")
    kind = schema.get("type")
    if kind:
        expected = _TYPES[kind]
        if isinstance(value, bool) and kind in ("integer", "number"):
            raise ToolError(f"{path} must be a {kind}, not a boolean")
        if kind == "integer" and isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, expected):
            raise ToolError(f"{path} must be {'an' if kind[0] in 'aeiou' else 'a'} {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError(f"{path} must be one of {', '.join(map(str, schema['enum']))}; got {value!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolError(f"{path} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolError(f"{path} must be at most {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ToolError(f"{path} must be greater than {schema['exclusiveMinimum']}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ToolError(f"{path} takes at most {schema['maxItems']} items")
        if "items" in schema:
            value = [_check(item, schema["items"], f"{path}[{i}]") for i, item in enumerate(value)]
    return value


def check_arguments(arguments: Any, schema: dict) -> dict:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object")
    properties = schema.get("properties", {})
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise ToolError(f"unknown argument(s): {', '.join(map(repr, unknown))}; allowed: {', '.join(properties)}")
    for name in schema.get("required", []):
        if name not in arguments:
            raise ToolError(f"missing required argument: {name}")
    return {name: _check(value, properties[name], name) for name, value in arguments.items()}


# --- the server ---

class StdioServer:
    def __init__(self, name: str, version: str, instructions: str, tools: list[Tool]):
        self.info = {"name": name, "version": version}
        self.instructions = instructions
        self.tools = {tool.name: tool for tool in tools}
        self.capabilities = {"tools": {"listChanged": False}}

    # Each handler returns a result dict; `modern` says whether to stamp it for the 2026 era.
    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        version = requested if requested in HANDSHAKE_VERSIONS else HANDSHAKE_VERSIONS[-1]
        return {"protocolVersion": version, "capabilities": self.capabilities,
                "serverInfo": self.info, "instructions": self.instructions}

    def _discover(self, params: dict) -> dict:
        return {"supportedVersions": [*HANDSHAKE_VERSIONS, *MODERN_VERSIONS], "capabilities": self.capabilities,
                "instructions": self.instructions}

    def _list_tools(self, params: dict) -> dict:
        return {"tools": [tool.describe() for tool in self.tools.values()]}

    def _call_tool(self, params: dict) -> dict:
        tool = self.tools.get(params.get("name"))
        if tool is None:
            raise RpcError(INVALID_PARAMS, f"unknown tool {params.get('name')!r}")
        try:
            arguments = check_arguments(params.get("arguments"), tool.input_schema)
            result = tool.handler(**arguments)
        except ToolError as error:
            return {"content": [{"type": "text", "text": str(error)}], "isError": True}
        except Exception as error:  # a bug or an outage: tell the agent what happened
            traceback.print_exc(file=sys.stderr)
            return {"content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}], "isError": True}
        if isinstance(result, ToolContent):
            out = {"content": result.blocks, "isError": False}
            if result.structured is not None:
                out["structuredContent"] = result.structured
            return out
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        return {"content": [{"type": "text", "text": text}], "structuredContent": result, "isError": False}

    def handle(self, message: Any) -> dict | None:
        """Answer one JSON-RPC message. Returns the response, or None for notifications."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            if isinstance(message, dict) and "method" not in message and ("result" in message or "error" in message):
                return None  # a response to something we never send; ignore
            return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "invalid request")
        request_id, method = message.get("id"), message["method"]
        params = message.get("params") or {}
        if "id" not in message:
            return None  # notifications (initialized, cancelled, ...) need no answer
        meta = params.get("_meta") if isinstance(params, dict) else None
        version = meta.get(PROTOCOL_VERSION_KEY) if isinstance(meta, dict) else None
        handlers = {"initialize": self._initialize, "server/discover": self._discover, "ping": lambda p: {},
                    "tools/list": self._list_tools, "tools/call": self._call_tool}
        handler = handlers.get(method)
        if handler is None:
            return _error(request_id, METHOD_NOT_FOUND, f"method not found: {method}")
        if version is not None and method != "initialize" and version not in (*HANDSHAKE_VERSIONS, *MODERN_VERSIONS):
            return _error(request_id, UNSUPPORTED_PROTOCOL_VERSION, f"unsupported protocol version {version!r}",
                          {"supported": [*HANDSHAKE_VERSIONS, *MODERN_VERSIONS], "requested": version})
        try:
            result = handler(params if isinstance(params, dict) else {})
        except RpcError as error:
            return _error(request_id, error.code, error.message, error.data)
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            return _error(request_id, INTERNAL_ERROR, f"{type(error).__name__}: {error}")
        if version in MODERN_VERSIONS:
            result["resultType"] = "complete"
            if method in CACHEABLE_METHODS:  # caching directives are required on these results
                result.setdefault("ttlMs", 0)
                result.setdefault("cacheScope", "private")
            result["_meta"] = {**(result.get("_meta") or {}), SERVER_INFO_KEY: dict(self.info)}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def run(self, stdin=None, stdout=None) -> None:
        """Serve newline-delimited JSON-RPC until stdin closes."""
        stdin = stdin or sys.stdin.buffer
        stdout = stdout or sys.stdout.buffer
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                response = _error(None, PARSE_ERROR, "parse error")
            else:
                try:
                    response = self.handle(message)
                except Exception as error:  # never let one message end the session
                    traceback.print_exc(file=sys.stderr)
                    response = _error(message.get("id") if isinstance(message, dict) else None,
                                      INTERNAL_ERROR, f"{type(error).__name__}: {error}")
            if response is not None:
                text = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
                # a lone surrogate (e.g. echoed from a request id) becomes a \udXXX escape: still valid JSON
                stdout.write(text.encode("utf-8", "backslashreplace") + b"\n")
                stdout.flush()


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}
