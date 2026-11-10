import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from winuae_mcp.rsp import RSP, RSPError
from winuae_mcp.server import Debugger, dispatch


class FakeWinUAE:
    """Wire-level emulator double, matching this fork's hex monitor replies."""
    def __enter__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.memory = bytearray(range(256)) * 64
        self.commands = []
        self.errors = []
        self.client = None
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        return self

    def reply(self, text):
        raw = text.encode()
        wire = b"$" + raw + b"#" + f"{sum(raw)%256:02x}".encode()
        # Exercise TCP fragmentation, including checksum splitting.
        for i in range(0, len(wire), 7):
            self.client.sendall(wire[i:i+7])

    def serve(self):
        try:
            self.client, _ = self.listener.accept()
            self.client.settimeout(3)
            while True:
                c = self.client.recv(1)
                if not c:
                    break
                if c == b"\x03":
                    self.reply("T02")
                if c != b"$":
                    continue
                raw = bytearray()
                while True:
                    c = self.client.recv(1)
                    if c == b"#":
                        break
                    raw.extend(c)
                checksum = self.client.recv(1) + self.client.recv(1)
                assert int(checksum, 16) == sum(raw) % 256
                self.client.sendall(b"+")
                cmd = raw.decode()
                self.commands.append(cmd)
                if cmd == "qSupported":
                    result = "PacketSize=10000"
                elif cmd == "?" or cmd == "s":
                    result = "T05"
                elif cmd == "c":
                    continue
                elif cmd == "g":
                    result = "".join(f"{n:08x}" for n in range(18))
                elif cmd.startswith("qRcmd,"):
                    text = bytes.fromhex(cmd[6:]).decode()
                    if text == "execution-status":
                        data = {"debugger_stopped": True, "execution": "stopped", "frame": 1}
                    elif text == "capabilities":
                        data = {"protocol": 1, "commands": ["checkpoint", "watch"]}
                    elif text.startswith("memory-check"):
                        data = {"writable_ram": True}
                    else:
                        data = {"text": text}
                    result = json.dumps(data).encode().hex()
                elif cmd.startswith("m"):
                    addr, length = (int(s, 16) for s in cmd[1:].split(","))
                    result = self.memory[addr:addr+length].hex()
                elif cmd.startswith("M"):
                    header, data = cmd[1:].split(":")
                    addr, length = (int(s, 16) for s in header.split(","))
                    self.memory[addr:addr+length] = bytes.fromhex(data)
                    result = "OK"
                elif cmd.startswith(("Z", "z", "P")) or cmd == "D":
                    result = "OK"
                else:
                    result = "E01"
                self.reply(result)
                if cmd == "D":
                    break
        except (OSError, AssertionError) as exc:
            self.errors.append(exc)

    def __exit__(self, *args):
        if self.client:
            self.client.close()
        self.listener.close()
        self.thread.join(4)


class BridgeTests(unittest.TestCase):
    def test_partial_packet_and_bad_checksum(self):
        a, b = socket.socketpair()
        b.settimeout(1)
        rsp = RSP(timeout=0.02)
        rsp.sock = a
        try:
            b.sendall(b"$T")
            with self.assertRaises(TimeoutError):
                rsp.packet()
            b.sendall(b"05#b9")
            self.assertEqual(rsp.packet(), "T05")
            self.assertEqual(b.recv(1), b"+")
            b.sendall(b"$OK#00")
            with self.assertRaises(RSPError):
                rsp.packet()
            self.assertEqual(b.recv(1), b"-")
        finally:
            rsp.close()
            b.close()

    def test_tool_failure_is_mcp_error(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Debugger(RSP(port=1), temp, "/tmp")
            response = dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "run", "arguments": {"seconds": -1}}}, db)
            self.assertTrue(response["result"]["isError"])

    def test_wire_debugger_workflow(self):
        with FakeWinUAE() as fake, tempfile.TemporaryDirectory() as temp:
            rsp = RSP(port=fake.port, timeout=2)
            db = Debugger(rsp, temp, r"C:\experiments")
            self.assertEqual(db.call("connect", {})["capabilities"]["protocol"], 1)
            self.assertEqual(db.call("read_registers", {})["pc"], 17)
            data = db.call("read_memory", {"address": 0, "length": 5000})
            self.assertEqual(len(bytes.fromhex(data["hex"])), 5000)
            db.call("snapshot_memory", {"name": "before", "address": 0, "length": 8})
            db.call("write_memory", {"address": 2, "hex_data": "ffff"})
            db.call("snapshot_memory", {"name": "after", "address": 0, "length": 8})
            diff = db.call("compare_snapshots", {"before": "before", "after": "after", "width": 2})
            self.assertEqual(diff["matches"], [{"address": 2, "before": 515, "after": 65535, "delta": 65020}])
            db.call("breakpoint", {"action": "add", "kind": "write", "address": 2, "length": 2})
            self.assertIn("Z2,2,2", fake.commands)
            self.assertFalse(db.call("run", {"mode": "step"})["budget_exhausted"])
            self.assertTrue(db.call("run", {"seconds": 0.02})["budget_exhausted"])
            self.assertEqual(db.call("read_registers", {})["sr"], 16)
            saved = db.call("checkpoint", {"action": "restore", "name": "shop"})
            self.assertTrue(saved["rearm_required"])
            self.assertEqual(saved["emulator_host_path"], r"C:\experiments\shop.uss")
            db.call("input_sequence", {"steps": [{"delay": 10, "kind": 3, "port": 0, "code": 0, "value": 0}]})
            self.assertIn("input sequence a 3 0 0 0", [bytes.fromhex(c[6:]).decode() for c in fake.commands if c.startswith("qRcmd,")])
            db.call("disconnect", {})
            self.assertIsNone(rsp.sock)

    def test_schema_validation_before_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Debugger(RSP(port=1), temp, "/tmp")
            cases = [("read_memory", {"address": -1, "length": 1}),
                     ("read_memory", {"address": True, "length": 1}),
                     ("checkpoint", {"name": "../escape", "action": "save"}),
                     ("write_memory", {"address": 0, "hex_data": "xyz"}),
                     ("run", {"seconds": 31}), ("run", {"unexpected": 1})]
            for name, args in cases:
                with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                    db.call(name, args)

    def test_mcp_dispatch(self):
        response = dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}, None)
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}, None))
        self.assertEqual(dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, None)["result"]["tools"][0]["name"], "connect")

    def test_stdio_process(self):
        with tempfile.TemporaryDirectory() as temp:
            requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                        {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
            proc = subprocess.run([sys.executable, "-m", "winuae_mcp.server", "--emulator-dir", temp, "--data-dir", temp], input="\n".join(map(json.dumps, requests))+"\n", capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            replies = [json.loads(line) for line in proc.stdout.splitlines()]
            self.assertEqual([r["id"] for r in replies], [1, 2])
            self.assertIn("compare_snapshots", {t["name"] for t in replies[1]["result"]["tools"]})


if __name__ == "__main__":
    unittest.main()
