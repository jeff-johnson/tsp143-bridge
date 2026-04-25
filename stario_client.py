#!/usr/bin/env python3
# Copyright (c) 2026 Senternal LLC <https://senternaltech.com>
# SPDX-License-Identifier: MIT
"""Standalone StarIO-protocol client for real-time error tracking.

Connects to a Star printer (or our bridge) on TCP 9100 the way Shopify's
StarIO SDK does: enables ASB, issues ETB checked blocks, and decodes every
status frame the printer returns. Prints a timestamped stream of state
changes so you can see exactly which ASB bit is triggering Shopify's
"check printer" warning.

Usage:
    ./stario_client.py HOST [--probe] [--watch SECONDS]

    --probe:  send a minimal print job (init + text + cut + ETB) and watch
              the ASB responses. Useful for reproducing Shopify's flow.
    --watch:  just listen for unsolicited ASB for N seconds (no writes).
"""

from __future__ import annotations

import argparse
import socket
import sys
import time

from stario_proto import StarStatus, StatusTracker, extract_frame

ESC = b"\x1b"
GS  = b"\x1d"
ETB = b"\x17"
CAN = b"\x18"

ENABLE_ASB  = ESC + b"\x1e\x61\x01"   # ESC RS a 1
DISABLE_ASB = ESC + b"\x1e\x61\x00"
STATUS_REQ  = ESC + b"\x06\x01"       # ESC ACK SOH (when ASB disabled)
CLEAR_ETB   = ESC + b"\x1e\x45\x00"   # ESC RS E 0


def ts() -> str:
    return time.strftime("%H:%M:%S", time.localtime()) + f".{int(time.time()*1000)%1000:03d}"


def log(tag: str, msg: str):
    print(f"{ts()} [{tag}] {msg}", flush=True)


def connect(host: str, port: int, timeout: float = 5.0) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


def read_frames(sock: socket.socket, tracker: StatusTracker,
                buf: bytearray, duration: float) -> int:
    """Read and decode ASB frames for `duration` seconds. Returns frame count."""
    sock.settimeout(0.25)
    end = time.time() + duration
    count = 0
    while time.time() < end:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                log("net", "peer closed")
                return count
            buf.extend(chunk)
        except socket.timeout:
            pass
        while True:
            frame, remainder = extract_frame(bytes(buf))
            if not frame:
                break
            buf[:] = remainder
            count += 1
            st, changes = tracker.update(frame)
            log("asb", f"{st.raw.hex()}  {st.summary()}")
            if changes and tracker.last and len(changes) <= 6:
                log("chg", " | ".join(changes))
    return count


def do_watch(host: str, port: int, seconds: float):
    sock = connect(host, port)
    log("net", f"connected to {host}:{port}")
    tracker = StatusTracker()
    buf = bytearray()
    try:
        sock.sendall(ENABLE_ASB)
        log("tx", f"ENABLE_ASB {ENABLE_ASB.hex()}")
        read_frames(sock, tracker, buf, seconds)
    finally:
        sock.close()
    log("end", f"final: {tracker.last.summary() if tracker.last else 'no frames'}")


def do_probe(host: str, port: int, seconds: float):
    sock = connect(host, port)
    log("net", f"connected to {host}:{port}")
    tracker = StatusTracker()
    buf = bytearray()
    try:
        sock.sendall(ENABLE_ASB)
        log("tx", f"ENABLE_ASB {ENABLE_ASB.hex()}")
        read_frames(sock, tracker, buf, 0.5)

        job = (
            ESC + b"@"                          # ESC @ — init
            + b"STARIO PROBE\n"
            + b"--- checked block 1 ---\n"
            + ETB                                # ETB — checked-block marker
        )
        sock.sendall(job)
        log("tx", f"job+ETB {len(job)} bytes")
        read_frames(sock, tracker, buf, 1.0)

        sock.sendall(b"block 2\n" + ETB)
        log("tx", "block2+ETB")
        read_frames(sock, tracker, buf, 1.0)

        sock.sendall(b"\n\n\n" + GS + b"V\x01")  # partial cut
        log("tx", "cut")
        read_frames(sock, tracker, buf, seconds)
    finally:
        sock.close()
    log("end", f"final: {tracker.last.summary() if tracker.last else 'no frames'}")


def main():
    ap = argparse.ArgumentParser(description="StarIO-protocol diagnostic client")
    ap.add_argument("host", help="Bridge or printer host (e.g. 127.0.0.1)")
    ap.add_argument("-p", "--port", type=int, default=9100)
    ap.add_argument("--probe", action="store_true",
                    help="Send a checked-block print job and watch ASB")
    ap.add_argument("--watch", type=float, default=10.0,
                    help="Seconds to watch for unsolicited ASB (default 10)")
    args = ap.parse_args()

    try:
        if args.probe:
            do_probe(args.host, args.port, args.watch)
        else:
            do_watch(args.host, args.port, args.watch)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
