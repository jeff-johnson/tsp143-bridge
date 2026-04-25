# StarPRNT Automatic Status Back (ASB) — TSP143IIILAN reference

This is the cross-cutting analysis of ASB framing on a real Star
TSP143IIILAN (chassis-stamped TSP100IIILAN, MAC `00:11:62:13:de:ef`,
firmware V2.2). It supplements the per-scenario decoded captures in
`research/print-simple/`, `research/asb-states/`, and `research/etb-sweep/`,
and is the canonical byte-level reference for the bridge's
`stario_proto.py` decoder and `print_server_bidir.py`'s ASB
USB↔LAN transformation.

ASB is the StarPRNT mechanism by which the printer pushes unsolicited
status updates back over an active TCP/9100 connection that has enabled
ASB (via `ESC RS a 1`). It is the printer's primary "real-time"
notification channel; status-port polling on 9101 is the slower
poll-respond complement.

## Frame format — LAN (11 bytes)

What you see when ASB-enabled traffic is captured between a real LAN
printer and a host on TCP/9100:

```
offset  byte  meaning
------  ----  ------------------------------------------------------
0       0x23  Frame header — bits 1-3 and 5-6 encode the frame size
              (decoded: USB frames are 9 bytes; 0x23 indicates 9 here
              even though the wire frame is 11 — the header always
              encodes the USB size, the LAN form just adds zero pad)
1       0x86  Version. Bit 7 = 1 ⇒ LAN format (USB form is 0x06).
              `print_server_bidir.usb_asb_to_lan_format()` sets this
              bit when forwarding USB ASB out over TCP.
2       ----  Printer status 1 (sticky-error and physical bits)
                bit 1  ETB EXECUTED — set in every ASB frame produced
                       in response to an ETB; cleared on the printer's
                       next idle ASB push.
                bit 3  OFFLINE — derived flag, set whenever any error
                       condition holds (cover open, paper out, mech
                       error, etc.).
                bit 5  COVER OPEN — physical sensor state.
                (other bits unused on this firmware/model)
3       0x00  Printer status 2 — mech error / cutter error / head overheat
              (Not exercised in our captures; expected when the printer
              has a hardware fault. Bridge code documents the bit
              positions.)
4       0x00  Printer status 3 — head up / paper jam / command error
              (Not exercised.)
5       ----  Sensor 1 — paper sensors
                bit 3  PAPER NEAR-END or PAPER OUT — single bit;
                       does not distinguish "low" from "out" on this
                       hardware.
6       0x00  Sensor 2 — presenter / slip detectors
              (Not present on TSP143IIILAN; reserved for higher-end
              models with a presenter or slip station.)
7       ----  ETB COUNTER (5 bits, non-contiguous — see below)
8       0x00  Reserved
9-10    0x00  LAN-format zero pad. Bridge appends these in
              `usb_asb_to_lan_format()` when forwarding from USB.
```

## Frame format — USB (9 bytes)

The same bytes 0-8 above, no trailing pad. Version byte 1 is `0x06`
instead of `0x86`. The bridge's `usb_asb_to_lan_format(usb_frame)`
function performs the USB→LAN conversion:

```python
def usb_asb_to_lan_format(usb_asb: bytes) -> bytes:
    """USB frame (9 bytes, version=0x06) → LAN frame (11 bytes, version=0x86)"""
    asb = bytearray(usb_asb)
    asb[1] |= 0x80           # set bit 7 of version byte
    while len(asb) < 11:     # pad to 11 bytes
        asb.append(0x00)
    return bytes(asb[:11])
```

This is the only byte-level difference between the two forms; semantics
are identical.

## ETB counter — 5-bit, non-contiguous, in byte 7

This is the most easily-misimplemented part of the ASB format. The
counter is **5 bits** (0–31, wraps to 0). Star scatters those 5 bits
across byte 7 in **non-adjacent** positions:

| counter bit | byte 7 bit |
|-------------|------------|
| 0 (LSB)     | 1          |
| 1           | 2          |
| 2           | 3          |
| 3           | 5          |
| 4 (MSB)     | 6          |

Byte 7 bits 0, 4, and 7 are **always zero** and reserved (treat them as
unset on encode; ignore on decode).

**Encoding:**
```python
byte7 = ((counter & 0x07) << 1) | ((counter & 0x18) << 2)
```

**Decoding:**
```python
counter = ((byte7 >> 1) & 0x07) | ((byte7 >> 2) & 0x18)
```

Verified byte-for-byte across 32 sequential ETBs in the `etb-sweep/`
capture. Selected pairs:

| counter | byte 7 | binary       |
|---------|--------|--------------|
| 0       | `0x00` | `0000_0000`  |
| 1       | `0x02` | `0000_0010`  |
| 7       | `0x0E` | `0000_1110`  |
| 8       | `0x20` | `0010_0000`  |
| 15      | `0x2E` | `0010_1110`  |
| 16      | `0x40` | `0100_0000`  |
| 23      | `0x4E` | `0100_1110`  |
| 24      | `0x60` | `0110_0000`  |
| 31      | `0x6E` | `0110_1110`  |
| (wrap) 0 | `0x00` | `0000_0000` |

After 32 ETBs, the printer's counter rolls over to 0 and the wire byte
returns to `0x00`. There is **no separate wrap flag** — clients tracking
ETBs across long sessions must handle the wraparound on the application
side. This matches what `stario_proto.py` already does.

The counter is reset by `ESC RS E 0` (CLEAR_ETB, `1b 1e 45 00`),
documented elsewhere in the bridge code.

## Observed flag combinations

From the `asb-states/` capture (cycling cover/paper states by hand):

| State | byte 2 | byte 5 | example frame |
|-------|--------|--------|---------------|
| Idle / clean | `0x00` | `0x00` | `23 86 00 00 00 00 00 00 00 00 00` |
| Cover open (paper still loaded) | `0x28` | `0x00` | `23 86 28 00 00 00 00 04 00 00 00` |
| Cover open + paper-sensor triggered | `0x28` | `0x08` | `23 86 28 00 00 08 00 04 00 00 00` |
| Paper out, cover closed | `0x08` | `0x08` | `23 86 08 00 00 08 00 04 00 00 00` |
| Post-ETB (clean except just-printed) | `0x02` | `0x00` | `23 86 02 00 00 00 00 02 00 00 00` |

`0x28 = 0010_1000` — bit 5 (cover open) + bit 3 (offline-derived).
`0x08 = 0000_1000` — bit 3 alone (offline without cover-open ⇒ another
fault, in this scenario paper-out).

The "offline" bit at byte 2 bit 3 is **derived from any error
condition** — it set whenever the printer can't print. Treat it as
informational only; the actual cause is in the more specific bits.

## Frame extraction — known parser bug

`stario_proto.extract_frame()` is currently hardcoded to consume exactly
9 bytes per frame, matching the USB form. When given LAN-form frames
(11 bytes each), it consumes only 9 and leaves 2 zero bytes in the
buffer; the next call sees `0x00 0x00 ...` which is not a valid frame
header and returns None, so the loop exits prematurely.

**Visible symptom:** `frames.log` in our LAN-side captures records only
the *first* ASB frame, even when the wire (and `raw.bin`) carry many.
The bridge's bidirectional forwarder doesn't suffer from this because
it operates on USB frames; only the standalone client-side decoder is
affected.

**Suggested fix (left for a follow-up PR):** branch in `extract_frame()`
on the version byte (offset 1):

```python
sz = 9 if buf[1] & 0x80 == 0 else 11
```

…and consume `sz` bytes per frame.

## Cross-references

- **Per-scenario byte dumps:** `research/print-simple/notes.md`,
  `research/asb-states/notes.md`, `research/etb-sweep/notes.md`
- **Bridge implementation:**
  - `../print_server_bidir.py` — `usb_asb_to_lan_format()`,
    cached-ASB serving on 9101
  - `../stario_proto.py` — `extract_frame()`, `StarStatus`, bit decode
  - `../README.md` lines 83–94 — the original ASB layout table this
    document refines
