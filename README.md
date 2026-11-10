# WinUAE MCP debugger server

A dependency-free Python 3.10+ stdio MCP server for the **reinauer/WinUAE
`back-to-the-roots` fork**, not stock WinUAE. No emulator changes are required.
Source compatibility was checked against commit
`51ff44a9c154692ed2d84efdf21655bd1fcfce5d`.

It connects to the fork's GDB remote server and translates typed MCP tool calls
into GDB packets and WinUAE JSON monitor commands. No AI provider, API key or
LangChain dependency is needed. The MCP client supplies the agent/model.

## 1. Configure WinUAE

Use a build of https://github.com/reinauer/WinUAE with `DEBUGGER` and
`GDBSERVER` enabled. In your `.uae` configuration:

```ini
debugging_features=gdbserver
gdb_port=2345
cpu_threaded=false
```

Start the game and release any host GUI pause. Attaching stops at the next CPU
debugger boundary. Create a directory on the **emulator host** for screenshots
and checkpoints, for example `C:\amiga-experiments` or `/home/ryan/amiga-experiments`.

The GDB listener is loopback-only and permits one client. Don't attach another
GDB client at the same time. For another machine, forward the port over SSH;
the GDB protocol itself has no authentication. `--host` refers to that endpoint.

## 2. Install

Extract this project, open a terminal in its directory, then:

```sh
python -m pip install .
```

Or run directly without installing anything:

```sh
PYTHONPATH=src python -m winuae_mcp.server --emulator-dir /home/ryan/amiga-experiments
```

The process waits for MCP requests on stdin. It does not start WinUAE. There is
no HTTP listener and stdout contains only newline-delimited JSON-RPC messages.

## 3. Configure your MCP client

Example configuration for a client supporting local stdio servers:

```json
{
  "mcpServers": {
    "winuae": {
      "command": "winuae-mcp",
      "args": [
        "--host", "127.0.0.1",
        "--port", "2345",
        "--emulator-dir", "C:\\amiga-experiments",
        "--data-dir", "C:\\amiga-analysis"
      ]
    }
  }
}
```

Replace paths for your system. Use the full path to `winuae-mcp` (or
`winuae-mcp.exe`) when your client does not inherit the installation PATH. To
avoid installation, set `command` to your Python executable, use arguments
`["-m", "winuae_mcp.server", "--emulator-dir", "..."]`, and set the client's
`PYTHONPATH` environment variable to the absolute project `src` directory.

`--data-dir` is on the **MCP server host**. It stores memory snapshots, defaulting
to `~/.winuae-mcp`. `--emulator-dir` is on the **WinUAE host**. Those paths may be
on different machines; screenshots return a host path rather than image bytes.

## Tools

| Tool | Purpose |
|---|---|
| `connect` | Attach, stop CPU, report capabilities and status |
| `disconnect` | Detach; guest resumes and remote breakpoints are removed |
| `status` | Inspect execution/frame/cycle state |
| `read_registers`, `write_register` | Inspect/change m68k integer registers |
| `read_memory`, `write_memory` | Read physical memory / write certified RAM |
| `disassemble` | Disassemble 1–100 instructions |
| `run` | Continue or step, with a 0.01–30 second wall-clock budget |
| `breakpoint` | Add/remove PC breakpoints and CPU data watchpoints |
| `checkpoint` | Save/restore a named WinUAE state file |
| `screenshot` | Write a named PNG on the emulator host |
| `input_sequence` | Queue frame-scheduled raw input events |
| `inspect` | Memory map, tasks, segments, exception, watches, input status |
| `monitor` | Advanced monitor operations including DMA watches/history |
| `snapshot_memory` | Save a named memory snapshot, up to 4 MiB |
| `compare_snapshots` | Find changed/increased/decreased/exact-delta values |

JSON addresses are **decimal integers**: Amiga `$10000` is `65536`.
Memory and register byte order is big-endian; addresses are physical, with no
guest MMU translation. Advanced `monitor` arguments follow WinUAE's hexadecimal
syntax, except documented exceptions such as decimal disassembly counts.

Calls are serialized. Most tools require the CPU stopped; they do not secretly
resume it. `run` waits for a stop or interrupts at its wall-clock budget. This
is not an exact emulated-frame execution limit. If the connection fails, the
guest's execution state may be unknown and WinUAE may resume on disconnect.
Mutations are never automatically retried.

The advanced monitor allowlist excludes operations that resume execution
(`step-over`, checkpoint saving) so they cannot leave unconsumed asynchronous
stop replies. Use typed execution/checkpoint tools instead. Advanced commands
that only read state or configure watches/inputs are supported.

## Example: Dragon's Breath purchase investigation

Give your agent this starting instruction:

> Connect to WinUAE. Inspect the memory map. Save a checkpoint named shop before
> making a purchase. Snapshot only valid RAM banks separately. Queue the raw
> mouse inputs needed to buy one ingredient, then run with a two-second budget.
> Capture a second snapshot and find big-endian 16-bit values that decreased by
> the displayed price. Restore shop, re-arm candidate write watchpoints, repeat
> the purchase and inspect registers, disassembly and watch events at the stop.
> Treat candidate addresses as hypotheses until different quantities validate them.

Example tool arguments:

```json
{"name":"breakpoint","arguments":{"action":"add","kind":"write","address":65536,"length":2}}
```

```json
{"name":"compare_snapshots","arguments":{"before":"before","after":"after","width":2,"relation":"delta","delta":-10,"limit":100}}
```

Snapshots must cover the same range. Comparisons use unsigned integers, do not
wrap deltas, and advance by `width` bytes relative to the snapshot's base address.
Results include the total match count and a truncation flag.

Input sequence steps contain `delay`, `kind`, `port`, `code`, `value`:

- `kind=0`: Amiga raw keyboard; port 0, code below `0x68`, value 0/1.
- `kind=1`: relative mouse; port 0/1, code 0=X or 1=Y, signed delta -127..127
  encoded as an unsigned 32-bit integer (e.g. -1 becomes 4294967295).
- `kind=2`: joystick/mouse buttons; port 0/1, code 0..6, value 0/1. Consult
  the fork's input implementation for the control mapping.
- `kind=3`: delay only; port, code and value must all be zero.

Delays are relative emulated frames. The fork allows at most 256 steps and
3600 frames in a sequence. Queuing inputs does not resume the CPU. Pausing
freezes the sequence; completion releases held keys/buttons. Mouse motion is
relative: restore a checkpoint and verify the cursor position before replay.

## Important checkpoint behaviour

- Saving resumes to the next frame boundary and stops again, advancing by up
  to one frame. Another breakpoint before completion can cancel the save.
- Restore preserves the GDB connection but clears remote breakpoints,
  watchpoints, exception settings and related capture state. Re-arm explicitly;
  this server does not restore those settings automatically.
- External disk/file writes are not rolled back. Use disposable disk copies for
  experiments that write files.
- Savestate support depends on the emulator build and active devices. Unsupported
  monitor operations are returned as MCP tool errors.
- Named snapshots/checkpoints overwrite earlier files with the same name.
- This server has no game-specific addresses, screen recognition or inferred
  symbol database. It supplies the tools for an agent to investigate those.

## Validation

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```

Tests exercise real TCP RSP framing against a simulated WinUAE endpoint,
fragmented replies, checksum rejection, partial-packet timeouts, reads/writes,
register layout, watchpoints, checkpoints, scheduled input, bounded execution,
snapshot comparisons, argument validation and a subprocess MCP stdio handshake.

No live WinUAE/ROM/game integration test was possible in the build environment.
First validate attachment, register reads and a single-step in your emulator
before running longer game experiments.

## Source references

- [Fork debugger guide](https://github.com/reinauer/WinUAE/blob/51ff44a9c154692ed2d84efdf21655bd1fcfce5d/README_unix.md#gdb-debugging)
- [Remote protocol](https://github.com/reinauer/WinUAE/blob/51ff44a9c154692ed2d84efdf21655bd1fcfce5d/gdb_protocol.cpp)
- [Monitor commands](https://github.com/reinauer/WinUAE/blob/51ff44a9c154692ed2d84efdf21655bd1fcfce5d/gdb_monitor.cpp)

The bridge is independent code and does not bundle emulator source, ROMs or games.
