"""Serialized GDB RSP client. Never retries a timed-out mutation."""
import json
import socket
import time


class RSPError(RuntimeError):
    pass


class RSP:
    def __init__(self, host="127.0.0.1", port=2345, timeout=5):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = None
        self.last_stop = None
        self._incoming = bytearray()

    def close(self):
        if self.sock:
            self.sock.close()
        self.sock = None
        self._incoming.clear()

    def connect(self):
        if self.sock:
            return
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        try:
            self.command("qSupported")
            self.last_stop = self.command("?")
        except Exception:
            self.close()
            raise

    def _byte(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("WinUAE reply timed out")
        self.sock.settimeout(remaining)
        try:
            b = self.sock.recv(1)
        except socket.timeout as exc:
            raise TimeoutError("WinUAE reply timed out") from exc
        if not b:
            self.close()
            raise RSPError("WinUAE disconnected; execution state is unknown")
        return b

    def packet(self, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            # Preserve partial packets across an execution-budget timeout.
            while b"$" not in self._incoming:
                c = self._byte(deadline)
                if c == b"-":
                    raise RSPError("WinUAE rejected the packet checksum")
                if c == b"$":
                    self._incoming.extend(c)
            while b"#" not in self._incoming:
                self._incoming.extend(self._byte(deadline))
                if len(self._incoming) > 131072:
                    raise RSPError("Oversized RSP packet")
            end = self._incoming.index(b"#")
            while len(self._incoming) < end + 3:
                self._incoming.extend(self._byte(deadline))
            wire = self._incoming[1:end]
            checksum = self._incoming[end+1:end+3]
            del self._incoming[:end+3]
            try:
                valid = int(checksum, 16) == sum(wire) % 256
            except ValueError:
                valid = False
            if not valid:
                self.sock.sendall(b"-")
                raise RSPError("Invalid reply checksum")
            self.sock.sendall(b"+")
            decoded = bytearray()
            i = 0
            while i < len(wire):
                c = wire[i]
                if c == 125:
                    i += 1
                    if i == len(wire):
                        raise RSPError("Invalid escape")
                    decoded.append(wire[i] ^ 32)
                elif c == 42:
                    i += 1
                    if not decoded or i == len(wire) or wire[i] < 29:
                        raise RSPError("Invalid run length")
                    decoded.extend([decoded[-1]] * (wire[i] - 29))
                else:
                    decoded.append(c)
                i += 1
            return decoded.decode("ascii")

    def send(self, payload):
        if not self.sock:
            raise RSPError("Not connected")
        data = payload.encode("ascii")
        self.sock.sendall(b"$" + data + b"#" + f"{sum(data) % 256:02x}".encode())

    def command(self, payload, timeout=None):
        self.send(payload)
        result = self.packet(timeout)
        if result.startswith("E"):
            raise RSPError(f"WinUAE error {result} for {payload.split(',')[0]}")
        if result == "":
            raise RSPError("Operation unsupported by this WinUAE build")
        return result

    def monitor(self, text, timeout=None):
        if not text or any(c in text for c in "\x00\r\n") or len(text.encode()) > 16000:
            raise ValueError("Invalid monitor command")
        result = self.command("qRcmd," + text.encode("utf-8").hex(), timeout)
        if result == "OK":
            return {"ok": True}
        decoded = bytes.fromhex(result).decode("utf-8")
        try:
            return json.loads(decoded)
        except json.JSONDecodeError:
            return {"text": decoded}

    def interrupt(self):
        self.sock.sendall(b"\x03")
        # A stop may already have arrived as the interrupt crossed it. Drain
        # that stop plus the interrupt response before issuing another command.
        self.last_stop = self.packet()
        if not self.last_stop.startswith(("T", "S")):
            raise RSPError("Expected stop response")
        # A harmless query is an ordered barrier: consume any duplicate stop
        # before its reply rather than guessing a TCP drain delay.
        self.send("qRcmd," + b"execution-status".hex())
        while True:
            extra = self.packet()
            if extra.startswith(("T", "S")):
                self.last_stop = extra
                continue
            if extra.startswith("E"):
                raise RSPError("Could not synchronize after interrupt")
            status = json.loads(bytes.fromhex(extra).decode())
            if not status.get("debugger_stopped"):
                raise RSPError("Guest did not stop after interrupt")
            break
        return self.last_stop

    def execute(self, command="c", seconds=2):
        self.send(command)
        try:
            stop = self.packet(seconds)
            if stop.startswith("E"):
                raise RSPError(stop)
            if not stop.startswith(("T", "S", "W", "X")):
                raise RSPError(f"Expected execution stop, received {stop}")
            self.last_stop = stop
            return {"stop": stop, "budget_exhausted": False}
        except TimeoutError:
            return {"stop": self.interrupt(), "budget_exhausted": True}
