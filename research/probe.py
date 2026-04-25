#!/usr/bin/env python3
# Copyright (c) 2026 Senternal LLC <https://senternaltech.com>
# SPDX-License-Identifier: MIT
"""Active probes for the tsp143-bridge research capture pack.

Drives a real Star LAN printer (or the bridge) through scripted scenarios
while a packet capture runs in parallel. Each mode writes:

    <out>/raw.bin       — every byte received from the printer (TCP modes)
    <out>/frames.log    — ASB frames, one per line, decoded
    <out>/probe.log     — timestamped high-level events for the scenario

Reuses framing helpers from ``../stario_client.py`` and ``../stario_proto.py``
so the wire format and decode logic match the bridge's own implementation.

Modes::

    simple          — init + 2 ETB checked blocks + partial cut
    drawer          — same as simple, plus cash-drawer kick (ESC p 0 25 250)
    etb-sweep       — 32 sequential checked blocks, watches ASB counter wrap
    hold-9101       — open status port 9101, hold idle, close
    watch-asb       — open :9100, ENABLE_ASB, drain forever (capture state transitions)
    discovery       — UDP STR_BCAST query to :22222, capture reply
    raster          — multi-KB ESC/POS raster image (synthetic bitmap)
    replay-shopify  — stream a captured Shopify receipt (binary file) to :9100
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from stario_client import (  # noqa: E402
    ESC, GS, ETB,
    ENABLE_ASB, CLEAR_ETB,
    connect, ts,
)
from stario_proto import StatusTracker, extract_frame  # noqa: E402


KICK_DRAWER = ESC + b"p\x00\x19\xfa"  # ESC p 0 25 250 — fire connector pin 2
PARTIAL_CUT = b"\n\n\n" + GS + b"V\x01"


class Recorder:
    """File-backed logger for one probe scenario."""

    def __init__(self, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        self._raw = open(out_dir / "raw.bin", "wb")
        self._frames = open(out_dir / "frames.log", "w")
        self._probe = open(out_dir / "probe.log", "w")
        self.tracker = StatusTracker()
        self.buf = bytearray()

    def log(self, tag: str, msg: str) -> None:
        line = f"{ts()} [{tag}] {msg}"
        print(line, flush=True)
        self._probe.write(line + "\n")
        self._probe.flush()

    def record_rx(self, chunk: bytes) -> None:
        self._raw.write(chunk)
        self._raw.flush()
        self.buf.extend(chunk)
        while True:
            frame, remainder = extract_frame(bytes(self.buf))
            if not frame:
                break
            self.buf[:] = remainder
            st, _ = self.tracker.update(frame)
            line = f"{ts()} {frame.hex()}  {st.summary()}"
            print(line, flush=True)
            self._frames.write(line + "\n")
            self._frames.flush()

    def close(self) -> None:
        for f in (self._raw, self._frames, self._probe):
            f.close()


def drain(sock: socket.socket, rec: Recorder, duration: float) -> None:
    sock.settimeout(0.25)
    end = time.time() + duration
    while time.time() < end:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            rec.log("net", "peer closed")
            return
        rec.record_rx(chunk)


def send(sock: socket.socket, rec: Recorder, data: bytes, label: str) -> None:
    sock.sendall(data)
    rec.log("tx", f"{label} ({len(data)} bytes)")


def mode_simple(host: str, port: int, rec: Recorder, hold: float) -> None:
    sock = connect(host, port)
    rec.log("net", f"connected to {host}:{port}")
    try:
        send(sock, rec, ENABLE_ASB, "ENABLE_ASB")
        drain(sock, rec, 0.5)
        send(sock, rec, ESC + b"@" + b"STARIO PROBE\n--- block 1 ---\n" + ETB, "block1+ETB")
        drain(sock, rec, 1.0)
        send(sock, rec, b"--- block 2 ---\n" + ETB, "block2+ETB")
        drain(sock, rec, 1.0)
        send(sock, rec, PARTIAL_CUT, "cut")
        drain(sock, rec, hold)
    finally:
        sock.close()


def mode_drawer(host: str, port: int, rec: Recorder, hold: float) -> None:
    sock = connect(host, port)
    rec.log("net", f"connected to {host}:{port}")
    try:
        send(sock, rec, ENABLE_ASB, "ENABLE_ASB")
        drain(sock, rec, 0.5)
        send(sock, rec, ESC + b"@" + b"DRAWER KICK PROBE\n" + ETB, "header+ETB")
        drain(sock, rec, 0.5)
        send(sock, rec, KICK_DRAWER, "KICK_DRAWER")
        drain(sock, rec, 1.0)
        send(sock, rec, PARTIAL_CUT, "cut")
        drain(sock, rec, hold)
    finally:
        sock.close()


def mode_etb_sweep(host: str, port: int, rec: Recorder, hold: float, blocks: int) -> None:
    sock = connect(host, port)
    rec.log("net", f"connected to {host}:{port}")
    try:
        send(sock, rec, ENABLE_ASB + CLEAR_ETB, "ENABLE_ASB+CLEAR_ETB")
        drain(sock, rec, 0.5)
        send(sock, rec, ESC + b"@" + b"ETB SWEEP\n", "init")
        for i in range(blocks):
            send(sock, rec, f"block {i + 1:02d}\n".encode() + ETB, f"block{i + 1}")
            drain(sock, rec, 0.3)
        send(sock, rec, PARTIAL_CUT, "cut")
        drain(sock, rec, hold)
    finally:
        sock.close()


def mode_hold_9101(host: str, port: int, rec: Recorder, hold: float) -> None:
    sock = connect(host, port)
    rec.log("net", f"connected to {host}:{port}")
    try:
        rec.log("hold", f"holding port {port} idle for {hold:.1f}s")
        drain(sock, rec, hold)
    finally:
        sock.close()
        rec.log("net", "closed")


def mode_watch_asb(host: str, port: int, rec: Recorder, hold: float) -> None:
    """Open :9100, enable ASB, listen for state-transition frames.

    Use this to capture the ASB byte layouts in different printer states:
    open the cover, close it, pull paper, reload, fire the drawer, etc.
    Each state transition produces an unsolicited ASB frame on this socket.
    """
    sock = connect(host, port)
    rec.log("net", f"connected to {host}:{port}")
    try:
        send(sock, rec, ENABLE_ASB, "ENABLE_ASB")
        rec.log("watch", f"capturing ASB transitions for {hold:.1f}s — go cycle states")
        drain(sock, rec, hold)
    finally:
        sock.close()
        rec.log("net", "closed")


def mode_discovery(host: str, rec: Recorder, hold: float) -> None:
    """Send a Star Discovery Protocol query to UDP/22222 and capture replies.

    `host` may be a unicast IP (targeted probe), a directed broadcast
    (e.g. 192.168.71.255 for /22), or 255.255.255.255.

    The query is the 28-byte structured form that real Star printers require.
    A naive ``b"STR_BCAST" + bytes(N)`` is *not* enough — real firmware
    ignores it. The bridge's responder accepts the loose form via substring
    match, but real hardware demands the structured form below::

        0x00  16 bytes  "STR_BCAST" + 7 NULs        (magic + pad)
        0x10   8 bytes  "RQ1.0.0" + 1 NUL           (request version)
        0x18   2 bytes  0x001C  (length, big-endian = 28)
        0x1A   2 bytes  request id (e.g. 0x6431)    (SDK sequence/handle)
    """
    query = (
        b"STR_BCAST" + b"\x00" * 7        # 16 bytes magic
        + b"RQ1.0.0\x00"                   # 8 bytes request version
        + b"\x00\x1C"                      # length = 28
        + b"\x64\x31"                      # request id
    )
    assert len(query) == 28, f"query must be 28 bytes, got {len(query)}"

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.25)
    sock.bind(("0.0.0.0", 0))
    rec.log("net", f"sending Star Discovery query (28B) to {host}:22222 from {sock.getsockname()}")
    sock.sendto(query, (host, 22222))
    end = time.time() + hold
    while time.time() < end:
        try:
            data, src = sock.recvfrom(2048)
        except socket.timeout:
            continue
        rec.log("rx", f"{len(data)} bytes from {src[0]}:{src[1]}")
        rec.record_rx(data)
    sock.close()


def mode_raster(host: str, port: int, rec: Recorder, hold: float, rows: int) -> None:
    """Send a synthetic raster image — checkerboard pattern, configurable height."""
    width_bytes = 72  # 576 dots = standard 80mm thermal width
    line = bytes([0x55, 0xaa] * (width_bytes // 2))[:width_bytes]
    raster = b"".join(
        # GS v 0 m xL xH yL yH data
        GS + b"v0\x00" + bytes([width_bytes & 0xFF, (width_bytes >> 8) & 0xFF, 1, 0]) + line
        for _ in range(rows)
    )
    sock = connect(host, port)
    rec.log("net", f"connected to {host}:{port}")
    try:
        send(sock, rec, ENABLE_ASB, "ENABLE_ASB")
        drain(sock, rec, 0.5)
        send(sock, rec, ESC + b"@" + b"RASTER PROBE\n", "init")
        send(sock, rec, raster, f"raster {rows} rows")
        send(sock, rec, b"\n" + ETB + PARTIAL_CUT, "ETB+cut")
        drain(sock, rec, hold)
    finally:
        sock.close()


def mode_replay_shopify(host: str, port: int, rec: Recorder, hold: float, fixture: Path) -> None:
    """Stream a captured Shopify-receipt binary verbatim to TCP/9100.

    Useful for testing the bridge or a real printer with the exact bytes a
    real Shopify POS sends, including its StarPRNT raster commands and ETB
    checked-block sequence. The fixture is the binary written by extracting
    the host->printer TCP/9100 stream from a real Shopify print capture.
    """
    payload = fixture.read_bytes()
    sock = connect(host, port)
    rec.log("net", f"connected to {host}:{port}")
    rec.log("replay", f"streaming {len(payload)} bytes from {fixture}")
    try:
        # Send in modest chunks to mimic real-app socket cadence and let the
        # printer respond with ASB frames between bursts.
        chunk = 4096
        for off in range(0, len(payload), chunk):
            sock.sendall(payload[off:off + chunk])
            drain(sock, rec, 0.05)
        rec.log("tx", "all bytes sent")
        drain(sock, rec, hold)
    finally:
        sock.close()


MODES = {
    "simple": lambda a, r: mode_simple(a.host, a.port, r, a.hold),
    "drawer": lambda a, r: mode_drawer(a.host, a.port, r, a.hold),
    "etb-sweep": lambda a, r: mode_etb_sweep(a.host, a.port, r, a.hold, a.blocks),
    "hold-9101": lambda a, r: mode_hold_9101(a.host, a.port, r, a.hold),
    "watch-asb": lambda a, r: mode_watch_asb(a.host, a.port, r, a.hold),
    "discovery": lambda a, r: mode_discovery(a.host, r, a.hold),
    "raster": lambda a, r: mode_raster(a.host, a.port, r, a.hold, a.rows),
    "replay-shopify": lambda a, r: mode_replay_shopify(a.host, a.port, r, a.hold, a.fixture),
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("mode", choices=sorted(MODES))
    ap.add_argument("host", help="Printer IP (or broadcast for discovery)")
    ap.add_argument("--port", type=int, default=9100,
                    help="TCP port (default 9100; 9101 for status modes)")
    ap.add_argument("--out", type=Path, required=True, help="Output directory")
    ap.add_argument("--hold", type=float, default=5.0,
                    help="Tail duration to keep listening / hold idle (seconds)")
    ap.add_argument("--blocks", type=int, default=32,
                    help="ETB sweep block count (etb-sweep only)")
    ap.add_argument("--rows", type=int, default=400,
                    help="Raster row count (raster only)")
    ap.add_argument("--fixture", type=Path,
                    default=HERE / "shopify-replay" / "sample-shopify-receipt.bin",
                    help="Receipt fixture file (replay-shopify only)")
    args = ap.parse_args()

    rec = Recorder(args.out)
    rec.log("start", f"mode={args.mode} host={args.host} port={args.port}")
    try:
        MODES[args.mode](args, rec)
    except KeyboardInterrupt:
        rec.log("end", "interrupted")
    except OSError as e:
        rec.log("end", f"error: {e}")
        rec.close()
        sys.exit(1)
    summary = rec.tracker.last.summary() if rec.tracker.last else "no frames"
    rec.log("end", f"final: {summary}")
    rec.close()


if __name__ == "__main__":
    main()
