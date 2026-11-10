"""MCP stdio (newline-delimited JSON-RPC), protocol versions 2024/2025."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from .rsp import RSP, RSPError

REGISTERS = [f"d{i}" for i in range(8)] + [f"a{i}" for i in range(8)] + ["sr", "pc"]
TOOLS = []


def field(kind, **extra):
    return {"type": kind, **extra}


def tool(name, description, properties=None, required=()):
    TOOLS.append({"name": name, "description": description,
                  "inputSchema": {"type": "object", "properties": properties or {},
                                  "required": list(required), "additionalProperties": False}})


A = field("integer", minimum=0, maximum=0xffffffff, description="Physical Amiga address, decimal JSON integer")
L = field("integer", minimum=1, maximum=65536)
NAME = field("string", pattern="^[A-Za-z0-9_-]{1,64}$")
SECONDS = field("number", minimum=0.01, maximum=30)
tool("connect", "Attach to WinUAE, stopping the CPU; return capabilities and execution status.")
tool("disconnect", "Detach and resume the guest; WinUAE removes remote breakpoints and watches.")
tool("status", "Get execution status without resuming the CPU.")
tool("read_registers", "Read d0-d7, a0-a7, sr, pc while stopped.")
tool("write_register", "Modify one CPU register while stopped.", {"register": field("string", enum=REGISTERS), "value": A}, ("register", "value"))
tool("read_memory", "Read bounded physical memory as hex; no MMU translation.", {"address": A, "length": L}, ("address", "length"))
tool("write_memory", "Write hex bytes to RAM after certifying the entire range. No hardware writes.", {"address": A, "hex_data": field("string", pattern="^(?:[0-9a-fA-F]{2}){1,4096}$")}, ("address", "hex_data"))
tool("disassemble", "Disassemble up to 100 instructions while stopped.", {"address": A, "count": field("integer", minimum=1, maximum=100)}, ("address",))
tool("run", "Continue or single-step; interrupt after a wall-clock budget. Returns stopped, unless connection fails.", {"mode": field("string", enum=["continue", "step"]), "seconds": SECONDS})
tool("breakpoint", "Add/remove a PC breakpoint or CPU read/write/access watchpoint.", {"action": field("string", enum=["add", "remove"]), "kind": field("string", enum=["pc", "write", "read", "access"]), "address": A, "length": L}, ("action", "kind", "address"))
tool("checkpoint", "Save/restore a named emulator-side .uss file. Save advances to a frame boundary; restore clears remote breakpoints/watches. External disk writes are not rolled back.", {"action": field("string", enum=["save", "restore"]), "name": NAME}, ("action", "name"))
tool("screenshot", "Write a PNG on the emulator host. Returns its host path, not image bytes.", {"name": NAME}, ("name",))
tool("input_sequence", "Queue up to 256 frame-scheduled raw inputs. Delays are relative emulated frames. Does not resume execution. kind: 0=keyboard, 1=relative mouse, 2=joystick, 3=delay. Values use WinUAE raw codes.", {"steps": field("array", minItems=1, maxItems=256, items={"type": "object", "properties": {k: field("integer", minimum=0, maximum=0xffffffff) for k in ["delay", "kind", "port", "code", "value"]}, "required": ["delay", "kind", "port", "code", "value"], "additionalProperties": False})}, ("steps",))
tool("inspect", "Read WinUAE structured debugger information while stopped.", {"subject": field("string", enum=["memory-map", "tasks", "segments", "exception", "watch events", "watch list", "input status"] )}, ("subject",))
tool("monitor", "Advanced WinUAE monitor command. Numeric arguments are HEX except documented exceptions. May mutate state. Prefer typed tools; execution-changing commands must use typed run/checkpoint tools.", {"command": field("string", minLength=1, maxLength=1024)}, ("command",))
tool("snapshot_memory", "Save up to 4 MiB of physical memory locally for comparisons. CPU must be stopped.", {"name": NAME, "address": A, "length": field("integer", minimum=1, maximum=4194304)}, ("name", "address", "length"))
tool("compare_snapshots", "Compare two same-range snapshots as big-endian unsigned integers, returning bounded candidate addresses.", {"before": NAME, "after": NAME, "width": field("integer", enum=[1, 2, 4]), "relation": field("string", enum=["changed", "unchanged", "increased", "decreased", "delta"]), "delta": field("integer"), "limit": field("integer", minimum=1, maximum=1000)}, ("before", "after"))


def validate(value, schema, path="arguments"):
    kind = schema.get("type")
    match = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": type(value) is int,
             "number": type(value) in (int, float)}
    if kind and not match[kind]:
        raise ValueError(f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: invalid choice")
    if kind in ("integer", "number"):
        if not schema.get("minimum", -float("inf")) <= value <= schema.get("maximum", float("inf")):
            raise ValueError(f"{path}: out of range")
    if kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 1000000):
            raise ValueError(f"{path}: invalid length")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ValueError(f"{path}: invalid format")
    if kind == "object":
        props = schema["properties"]
        if set(schema.get("required", [])) - value.keys():
            raise ValueError(f"{path}: missing required arguments")
        if schema.get("additionalProperties") is False and value.keys() - props.keys():
            raise ValueError(f"{path}: unknown arguments")
        for key, item in value.items():
            validate(item, props[key], f"{path}.{key}")
    if kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 1000000):
            raise ValueError(f"{path}: invalid item count")
        for item in value:
            validate(item, schema["items"], path + "[]")


class Debugger:
    def __init__(self, rsp, data_dir, emulator_dir):
        self.rsp = rsp
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.emulator_dir = emulator_dir.rstrip("/\\")

    def stopped(self):
        self.rsp.connect()
        status = self.rsp.monitor("execution-status")
        if not status.get("debugger_stopped") or status.get("execution") == "host-paused":
            raise RSPError("CPU must be debugger-stopped, with host UI pause released")

    def host_path(self, name, suffix):
        sep = "\\" if "\\" in self.emulator_dir else "/"
        return self.emulator_dir + sep + name + suffix

    def memory(self, address, length):
        if address + length > 2**32:
            raise ValueError("Memory range wraps the physical address space")
        output = bytearray()
        for offset in range(0, length, 4096):
            count = min(4096, length - offset)
            chunk = bytes.fromhex(self.rsp.command(f"m{address+offset:x},{count:x}"))
            if len(chunk) != count:
                raise RSPError("Short memory read")
            output.extend(chunk)
        return bytes(output)

    def call(self, name, args):
        spec = next((t for t in TOOLS if t["name"] == name), None)
        if not spec:
            raise ValueError("Unknown tool")
        validate(args, spec["inputSchema"])
        if name == "compare_snapshots":
            return self.compare(**args)
        if name == "disconnect":
            if self.rsp.sock:
                try:
                    self.rsp.command("D")
                finally:
                    self.rsp.close()
            return {"detached": True, "guest_resumes": True}
        self.rsp.connect()
        if name == "connect":
            return {"capabilities": self.rsp.monitor("capabilities"), "status": self.rsp.monitor("execution-status")}
        if name == "status":
            return self.rsp.monitor("execution-status")
        self.stopped()
        if name == "read_registers":
            raw = self.rsp.command("g")
            if len(raw) != 144:
                raise RSPError("Unexpected m68k register layout")
            return {reg: int(raw[i*8:i*8+8], 16) for i, reg in enumerate(REGISTERS)}
        if name == "write_register":
            return {"reply": self.rsp.command(f"P{REGISTERS.index(args['register']):x}={args['value']:08x}")}
        if name == "read_memory":
            return {**args, "hex": self.memory(**args).hex(), "byte_order": "big"}
        if name == "write_memory":
            data = bytes.fromhex(args["hex_data"])
            address = args["address"]
            if address + len(data) > 2**32:
                raise ValueError("Memory range wraps")
            self.rsp.monitor(f"memory-check {address:x} {len(data):x}")
            return {"reply": self.rsp.command(f"M{address:x},{len(data):x}:{data.hex()}")}
        if name == "disassemble":
            return self.rsp.monitor(f"disasm {args['address']:x} {args.get('count', 16)}")
        if name == "run":
            return self.rsp.execute("s" if args.get("mode") == "step" else "c", args.get("seconds", 2))
        if name == "breakpoint":
            typ = {"pc": 0, "write": 2, "read": 3, "access": 4}[args["kind"]]
            length = args.get("length", 2 if typ == 0 else 1)
            reply = self.rsp.command(f"{'Z' if args['action']=='add' else 'z'}{typ},{args['address']:x},{length:x}")
            return {"reply": reply, **args, "length": length}
        if name in ("checkpoint", "screenshot"):
            path = self.host_path(args["name"], ".uss" if name == "checkpoint" else ".png")
            command = f"checkpoint {args['action']} {path}" if name == "checkpoint" else f"screenshot {path}"
            result = self.rsp.monitor(command, timeout=30)
            return {"result": result, "emulator_host_path": path, "rearm_required": name == "checkpoint" and args["action"] == "restore"}
        if name == "input_sequence":
            rows = []
            for step in args["steps"]:
                if step["kind"] > 3:
                    raise ValueError("Input kind must be 0..3")
                rows.append(" ".join(f"{step[k]:x}" for k in ["delay", "kind", "port", "code", "value"]))
            return self.rsp.monitor("input sequence " + ";".join(rows))
        if name == "inspect":
            return self.rsp.monitor(args["subject"])
        if name == "monitor":
            command = args["command"].strip()
            allowed = {"capabilities", "execution-status", "memory-map", "memory-check", "disasm", "process-break", "segments", "tasks", "history", "condition", "exception", "exception-mask", "dma-watch", "watch", "guest-output", "input"}
            if not command or command.split()[0] not in allowed:
                raise ValueError("Monitor command not allowed; use typed execution/checkpoint/screenshot tools")
            return self.rsp.monitor(command)
        if name == "snapshot_memory":
            data = self.memory(args["address"], args["length"])
            digest = hashlib.sha256(data).hexdigest()
            # One self-contained file, atomically replaced; no mismatched metadata.
            result = {**args, "sha256": digest, "hex": data.hex()}
            dest = self.data_dir / (args["name"] + ".json")
            temp = dest.with_suffix(".tmp")
            temp.write_text(json.dumps(result), encoding="utf-8")
            os.replace(temp, dest)
            return {k: v for k, v in result.items() if k != "hex"}
        raise ValueError("Tool not implemented")

    def compare(self, before, after, width=2, relation="changed", delta=0, limit=100):
        a, b = [json.loads((self.data_dir / (n + ".json")).read_text()) for n in (before, after)]
        if (a["address"], a["length"]) != (b["address"], b["length"]):
            raise ValueError("Snapshots must cover the same range")
        aa, bb = bytes.fromhex(a["hex"]), bytes.fromhex(b["hex"])
        matches, total = [], 0
        for offset in range(0, len(aa) - width + 1, width):
            x, y = int.from_bytes(aa[offset:offset+width], "big"), int.from_bytes(bb[offset:offset+width], "big")
            hit = {"changed": x != y, "unchanged": x == y, "increased": y > x, "decreased": y < x, "delta": y-x == delta}[relation]
            if hit:
                total += 1
                if len(matches) < limit:
                    matches.append({"address": a["address"]+offset, "before": x, "after": y, "delta": y-x})
        return {"matches": matches, "total_matches": total, "truncated": total > limit, "width": width, "alignment_base": a["address"], "byte_order": "big"}


def dispatch(message, debugger):
    request_id = message.get("id")
    if "id" not in message:
        return None
    method, params = message.get("method"), message.get("params", {})
    if message.get("jsonrpc") != "2.0" or not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "Invalid request"}}
    if method == "initialize":
        version = params.get("protocolVersion")
        result = {"protocolVersion": version if version in ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"] else "2024-11-05", "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "winuae-mcp", "version": "0.1.0"}, "instructions": "Physical big-endian Amiga debugger. Attach stops CPU. Use bounded run. Restores clear watches. Disconnect resumes guest."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            data = debugger.call(params.get("name"), params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(data)}], "isError": False}
        except Exception as exc:
            # Drop a potentially desynchronised stream. Never retry mutations.
            if isinstance(exc, (OSError, TimeoutError, RSPError)):
                debugger.rsp.close()
            result = {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2345)
    parser.add_argument("--data-dir", default=str(Path.home() / ".winuae-mcp"))
    parser.add_argument("--emulator-dir", required=True, help="Existing directory on the WinUAE host for screenshots/checkpoints")
    args = parser.parse_args()
    debugger = Debugger(RSP(args.host, args.port), args.data_dir, args.emulator_dir)
    try:
        for line in sys.stdin.buffer:
            try:
                if len(line) > 2**20:
                    raise ValueError("Request too large")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Expected one JSON-RPC object")
                response = dispatch(message, debugger)
            except (ValueError, TypeError) as exc:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
            if response is not None:
                print(json.dumps(response), flush=True)
    finally:
        # WinUAE resumes and removes remote watches on disconnect.
        debugger.rsp.close()


if __name__ == "__main__":
    main()
