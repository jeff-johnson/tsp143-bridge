# Copyright (c) 2026 Senternal LLC <https://senternaltech.com>
# SPDX-License-Identifier: MIT
"""Star Line Mode ASB protocol parser and state tracker.

Shared between the bridge (for observer logging) and the standalone probe.
Spec: StarLine CM manual §5.2 (Automatic Status), asb.pdf.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


def asb_size(header: int) -> int:
    """Decode frame size from Header-1 byte (bits 1-3, bit 5)."""
    return ((header & 0b00001110) >> 1) | ((header & 0b00100000) >> 2)


def extract_frame(buf: bytes) -> tuple[Optional[bytes], bytes]:
    """Peel one ASB frame off the front of buf, or return (None, buf).

    Handles both wire forms:
    - USB:  9-byte body, version byte (offset 1) high bit clear (0x06).
    - LAN: 11-byte wire form — same 9-byte body plus 2 trailing zero
           bytes that the LAN-format printer appends. Version byte high
           bit set (0x86).

    The size field in byte 0 always encodes the USB-form length. We
    detect LAN form via the version-byte high bit and consume the extra
    2 pad bytes from the buffer too — otherwise the next call would see
    a leading 0x00 (no frame marker) and bail, dropping every frame
    after the first.
    """
    if not buf or not buf[0] & 0x01:
        return None, buf
    n = asb_size(buf[0])
    if n < 2 or len(buf) < n:
        return None, buf
    # LAN form: 2 trailing zero bytes append after the USB body.
    if buf[1] & 0x80:
        wire_n = n + 2
        if len(buf) < wire_n:
            return None, buf
        return bytes(buf[:wire_n]), bytes(buf[wire_n:])
    return bytes(buf[:n]), bytes(buf[n:])


def decode_etb_counter(byte7: int) -> int:
    """Byte 7 layout: 0 c0 c1 c2 0 c3 c4 0 → reconstruct 5-bit counter."""
    return (
        ((byte7 >> 1) & 0x01) |
        ((byte7 >> 1) & 0x02) |
        ((byte7 >> 1) & 0x04) |
        ((byte7 >> 2) & 0x08) |
        ((byte7 >> 2) & 0x10)
    )


@dataclass
class StarStatus:
    """Decoded ASB frame. Field names match StarLine CM bit descriptions."""
    raw: bytes = b""
    version: int = 0
    size: int = 0
    etb_executed: bool = False
    switch_closed: bool = False
    offline: bool = False
    cover_open: bool = False
    pressed_offline: bool = False
    mech_error: bool = False
    cutter_error: bool = False
    unrecoverable_error: bool = False
    head_hot: bool = False
    head_up_error: bool = False
    presenter_jam: bool = False
    paper_size_error: bool = False
    command_error: bool = False
    rx_overflow: bool = False
    paper_outer_near_end: bool = False
    paper_inner_near_end: bool = False
    paper_out: bool = False
    presenter_paper: bool = False
    slip_tof_no_paper: bool = False
    slip_bof_no_paper: bool = False
    etb_counter: int = 0

    @classmethod
    def parse(cls, frame: bytes) -> "StarStatus":
        s = cls(raw=frame)
        if len(frame) < 2:
            return s
        s.size = asb_size(frame[0])
        b1 = frame[1]
        s.version = ((b1 >> 1) & 0x07) | ((b1 >> 2) & 0x18)
        if len(frame) >= 3:
            b = frame[2]
            s.etb_executed   = bool(b & 0x02)
            s.switch_closed  = bool(b & 0x04)
            s.offline        = bool(b & 0x08)
            s.cover_open     = bool(b & 0x20)
            s.pressed_offline= bool(b & 0x40)
        if len(frame) >= 4:
            b = frame[3]
            s.mech_error          = bool(b & 0x04)
            s.cutter_error        = bool(b & 0x08)
            s.unrecoverable_error = bool(b & 0x20)
            s.head_hot            = bool(b & 0x40)
        if len(frame) >= 5:
            b = frame[4]
            s.head_up_error    = bool(b & 0x02)
            s.presenter_jam    = bool(b & 0x04)
            s.paper_size_error = bool(b & 0x08)
            s.command_error    = bool(b & 0x20)
            s.rx_overflow      = bool(b & 0x40)
        if len(frame) >= 6:
            b = frame[5]
            s.paper_outer_near_end = bool(b & 0x02)
            s.paper_inner_near_end = bool(b & 0x04)
            s.paper_out            = bool(b & 0x08)
        if len(frame) >= 7:
            b = frame[6]
            s.presenter_paper    = bool(b & 0x02)
            s.slip_tof_no_paper  = bool(b & 0x04)
            s.slip_bof_no_paper  = bool(b & 0x08)
        if len(frame) >= 8:
            s.etb_counter = decode_etb_counter(frame[7])
        return s

    def errors(self) -> list[str]:
        """Return the list of flags that would make StarIO surface an error."""
        flags = []
        for name in (
            "offline", "cover_open", "mech_error", "cutter_error",
            "unrecoverable_error", "head_hot", "head_up_error",
            "presenter_jam", "paper_size_error", "command_error",
            "rx_overflow", "paper_out", "paper_inner_near_end",
            "paper_outer_near_end",
        ):
            if getattr(self, name):
                flags.append(name)
        return flags

    def summary(self) -> str:
        errs = self.errors()
        base = f"v{self.version} sz={self.size} etb={self.etb_counter}"
        if self.etb_executed:
            base += " +etb_exec"
        return base + (" ERR=[" + ",".join(errs) + "]" if errs else " clean")


def diff(a: StarStatus, b: StarStatus) -> list[str]:
    """Report which flag fields changed a→b."""
    changes = []
    da, db = asdict(a), asdict(b)
    for k in da:
        if k == "raw":
            continue
        if da[k] != db[k]:
            changes.append(f"{k}: {da[k]}→{db[k]}")
    return changes


class StatusTracker:
    """Holds last-seen status; emits only when something changes."""

    def __init__(self, label: str = ""):
        self.label = label
        self.last: Optional[StarStatus] = None

    def update(self, frame: bytes) -> tuple[StarStatus, list[str]]:
        """Parse frame, return (status, changed_fields_vs_last)."""
        st = StarStatus.parse(frame)
        changes = diff(self.last, st) if self.last else ["(initial)"]
        self.last = st
        return st, changes
