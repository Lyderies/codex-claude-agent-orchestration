"""Narrow synthetic-job stdio reader; the operator supplies the OS sandbox.

Standalone stdlib module, copied into a private job and run with -I -S -B.
No provider, authentication, network, command or arbitrary-path interface exists.
Tool annotations are descriptive only. Hook success continues the owner's static
allowlist; it never grants permission. This module does not attest containment.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import sys

MAX_ARTIFACT = 16 * 1024
MAX_MESSAGE = 16 * 1024  # Inbound bytes, including the JSONL newline.
MAX_MESSAGES = 64
MAX_RESPONSE = 6 * MAX_ARTIFACT + 4096  # Exact text may expand during JSON escaping.
PROTOCOL = "2025-06-18"
MODEL = "claude-haiku-5-5"
MODELS = (MODEL, "claude-sonnet-5-5")
ROLES = ("claude_agents_implementation_worker", "claude_agents_verification_worker")
READ_SLOTS = tuple(f".job-read-{number}.slot" for number in range(1, 5))
ROLE_SLOTS = (".job-agent-worker.slot", ".job-agent-verifier.slot")
ARTIFACTS = ("source", "candidate")
ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
TOOL_JSON = (
    '{"name":"read_artifact","description":"Read one named synthetic job artifact.",'
    '"inputSchema":{"type":"object","properties":{"artifact":{"type":"string",'
    '"enum":["source","candidate"]}},"required":["artifact"],'
    '"additionalProperties":false},"annotations":{"readOnlyHint":true,'
    '"openWorldHint":false}}'
)


class JobReadError(Exception):
    """Fixed, content-free denial; never include OS errors or caller data."""

    def __init__(self) -> None:
        super().__init__("job_read_denied")


def _expected_model(value: object) -> str:
    if type(value) is not str or value not in MODELS:
        raise JobReadError()
    return value


class _RPCError(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


def _load(raw: bytes) -> object:
    if type(raw) is not bytes or not raw or len(raw) > MAX_MESSAGE:
        raise JobReadError()

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise JobReadError()
            result[key] = value
        return result

    def nonfinite(_value):
        raise JobReadError()

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=nonfinite)
        pending = [(value, 0)]
        nodes = 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if depth > 16 or nodes > 2048:
                raise JobReadError()
            if type(item) is dict:
                pending.extend((v, depth + 1) for v in item.values())
                pending.extend((k, depth + 1) for k in item)
            elif type(item) is list:
                pending.extend((v, depth + 1) for v in item)
            elif type(item) is str:
                item.encode("utf-8")  # Reject escaped unpaired surrogates.
            elif type(item) in (int, float):
                if not math.isfinite(item) or abs(item) > 2**53 - 1:
                    raise JobReadError()
            elif item is not None and type(item) is not bool:
                raise JobReadError()
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise JobReadError() from None


def _root_ok(fd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise JobReadError()


def _open_root(root: str) -> int:
    if (type(root) is not str or len(root) > 4096 or not root.startswith("/")
            or any(part in ("", ".", "..") for part in root[1:].split("/"))
            or any(ord(char) < 32 for char in root)):
        raise JobReadError()
    fd = -1
    try:
        fd = os.open("/", DIR_FLAGS)
        for component in root[1:].split("/"):
            child = os.open(component, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        _root_ok(fd)
        return fd
    except (OSError, ValueError, JobReadError):
        if fd >= 0:
            os.close(fd)
        raise JobReadError() from None


def _snapshot(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _file_ok(info: os.stat_result) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or not 0 <= info.st_size <= MAX_ARTIFACT
            or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)):
        raise JobReadError()


class JobReadTool:
    """Trusted operator root, fixed filenames, durable non-refundable counters."""

    def __init__(self, root: str) -> None:
        self._fd = _open_root(root)
        self._state = 0

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        self.close()

    def _reserve(self, names: tuple[str, ...]) -> None:
        try:
            _root_ok(self._fd)
            for name in names:
                try:
                    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self._fd)
                except FileExistsError:
                    continue  # Even an incomplete/unsafe occupied slot stays spent.
                try:
                    if os.write(fd, b"reserved\n") != 9:
                        raise JobReadError()
                    os.fsync(fd)
                    os.fsync(self._fd)
                    _root_ok(self._fd)
                finally:
                    os.close(fd)
                return
        except OSError:
            raise JobReadError() from None
        raise JobReadError()

    def _read(self, artifact: str) -> str:
        if type(artifact) is not str or artifact not in ARTIFACTS:
            raise JobReadError()
        fd = other = -1
        try:
            _root_ok(self._fd)
            name = "source.txt" if artifact == "source" else "candidate.txt"
            fd = os.open(name, FILE_FLAGS, dir_fd=self._fd)
            before = os.fstat(fd)
            _file_ok(before)
            data = bytearray()
            while len(data) <= MAX_ARTIFACT:
                chunk = os.read(fd, MAX_ARTIFACT + 1 - len(data))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) != before.st_size or len(data) > MAX_ARTIFACT:
                raise JobReadError()
            if _snapshot(before) != _snapshot(os.fstat(fd)):
                raise JobReadError()
            other = os.open(name, FILE_FLAGS, dir_fd=self._fd)
            if _snapshot(before) != _snapshot(os.fstat(other)):
                raise JobReadError()
            _root_ok(self._fd)
            return data.decode("utf-8")
        except (OSError, UnicodeError):
            raise JobReadError() from None
        finally:
            if other >= 0:
                os.close(other)
            if fd >= 0:
                os.close(fd)

    def read_artifact(self, arguments: object) -> str:
        self._reserve(READ_SLOTS)
        if type(arguments) is not dict or set(arguments) != {"artifact"}:
            raise JobReadError()
        return self._read(arguments["artifact"])

    def guard_agent(self, raw: bytes, *, expected_model: str = MODEL) -> None:
        expected_model = _expected_model(expected_model)
        event = _load(raw)
        if (type(event) is not dict or event.get("hook_event_name") != "PreToolUse"
                or event.get("tool_name") != "Agent"):
            raise JobReadError()
        for field in ("session_id", "tool_use_id"):
            if type(event.get(field)) is not str or ID.fullmatch(event[field]) is None:
                raise JobReadError()
        inputs = event.get("tool_input")
        required = {"subagent_type", "prompt", "description"}
        if (type(inputs) is not dict or not required <= set(inputs)
                or set(inputs) - required - {"model"}):
            raise JobReadError()
        role = inputs["subagent_type"]
        if type(role) is not str or role not in ROLES:
            raise JobReadError()
        for name, limit in (("prompt", 8192), ("description", 160)):
            text = inputs[name]
            if type(text) is not str or not text.strip() or len(text.encode("utf-8")) > limit:
                raise JobReadError()
        if "model" in inputs and (type(inputs["model"]) is not str or inputs["model"] != expected_model):
            raise JobReadError()
        if role == ROLES[1]:
            self._read("candidate")  # Existence/safety check only; no content output.
        self._reserve((ROLE_SLOTS[ROLES.index(role)],))
        # No JSON approval, rewritten arguments, or model-authored authority.

    def _dispatch(self, method: str, params: dict, notification: bool) -> dict | None:
        if method == "notifications/initialized":
            if not notification or params or self._state != 1:
                raise _RPCError(-32600)
            self._state = 2
            return None
        if notification:
            raise _RPCError(-32600)
        if method == "ping":
            if params:
                raise _RPCError(-32602)
            return {}
        if method == "initialize":
            if self._state != 0:
                raise _RPCError(-32600)
            if (set(params) - {"protocolVersion", "capabilities", "clientInfo", "_meta"}
                    or type(params.get("protocolVersion")) is not str
                    or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", params["protocolVersion"]) is None
                    or type(params.get("capabilities")) is not dict
                    or type(params.get("clientInfo")) is not dict):
                raise _RPCError(-32602)
            info = params["clientInfo"]
            if any(type(info.get(key)) is not str or not 1 <= len(info[key]) <= 128
                   for key in ("name", "version")):
                raise _RPCError(-32602)
            self._state = 1
            return {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                    "serverInfo": {"name": "synthetic-job-reader", "version": "1"}}
        if method not in ("tools/list", "tools/call"):
            raise _RPCError(-32601)
        if self._state != 2:
            raise _RPCError(-32600)
        if method == "tools/list":
            if set(params) - {"_meta"}:
                raise _RPCError(-32602)
            return {"tools": [json.loads(TOOL_JSON)]}  # Fresh immutable-schema copy.
        try:
            self._reserve(READ_SLOTS)  # Bad calls spend slots too; never refunded.
            if (set(params) - {"name", "arguments", "_meta"}
                    or params.get("name") != "read_artifact"
                    or type(params.get("arguments")) is not dict
                    or set(params["arguments"]) != {"artifact"}):
                raise _RPCError(-32602)
            text = self._read(params["arguments"]["artifact"])
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except JobReadError:
            return {"content": [{"type": "text", "text": "job_read_denied"}], "isError": True}

    def handle_line(self, raw: bytes) -> bytes | None:
        identity = None
        notification = False
        try:
            request = _load(raw)
            if (type(request) is not dict or set(request) - {"jsonrpc", "id", "method", "params"}
                    or request.get("jsonrpc") != "2.0"
                    or type(request.get("method")) is not str):
                raise _RPCError(-32600)
            notification = "id" not in request
            if not notification:
                value = request["id"]
                if not ((type(value) is int and 0 <= value <= 2**31 - 1)
                        or (type(value) is str and ID.fullmatch(value))):
                    raise _RPCError(-32600)
                identity = value
            params = request.get("params", {})
            if type(params) is not dict:
                raise _RPCError(-32602)
            result = self._dispatch(request["method"], params, notification)
            if notification:
                return None
            response = {"jsonrpc": "2.0", "id": identity, "result": result}
        except (JobReadError, _RPCError) as error:
            if notification:
                return None
            code = error.code if type(error) is _RPCError else -32700
            message = {-32700: "invalid_json", -32600: "invalid_request",
                       -32601: "unknown_method", -32602: "invalid_params"}[code]
            response = {"jsonrpc": "2.0", "id": identity,
                        "error": {"code": code, "message": message}}
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > MAX_RESPONSE:
            raise JobReadError()
        return encoded


def serve(root: str, reader, writer) -> None:
    with JobReadTool(root) as tool:
        for _ in range(MAX_MESSAGES):
            line = reader.readline(MAX_MESSAGE + 1)
            if not line:
                return
            if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
                writer.write(b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"invalid_json"}}\n')
                writer.flush()
                return
            response = tool.handle_line(line)
            if response is not None:
                writer.write(response)
                writer.flush()


def main(argv=None, reader=None, writer=None, errors=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    reader = sys.stdin.buffer if reader is None else reader
    writer = sys.stdout.buffer if writer is None else writer
    errors = sys.stderr.buffer if errors is None else errors
    guard = len(args) in (3, 5) and args[2] == "--guard-agent"
    if (not (len(args) == 2 or guard) or args[0] != "--root"
            or (len(args) == 5 and args[3] != "--expected-model")):
        errors.write(b"job_reader_denied\n")
        return 2
    try:
        if guard:
            expected_model = _expected_model(args[4] if len(args) == 5 else MODEL)
            with JobReadTool(args[1]) as tool:
                tool.guard_agent(reader.read(MAX_MESSAGE + 1), expected_model=expected_model)
        else:
            serve(args[1], reader, writer)
        return 0
    except (JobReadError, OSError, ValueError):
        errors.write(b"job_reader_denied\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
