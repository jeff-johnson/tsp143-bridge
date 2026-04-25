# etb-sweep — ETB counter byte-encoding fully decoded

This scenario sends 32 sequential checked-blocks (each ending in `ETB`,
`0x17`) and observes how the printer's ETB counter — embedded in byte 7
of every ASB frame — increments and eventually wraps.

## The protocol-level finding

The ETB counter is **5 bits** (range 0–31, wraps to 0). It is NOT
written contiguously into byte 7 — Star scatters it across 5
non-adjacent bit positions:

| counter bit | byte 7 bit |
|-------------|------------|
| 0 (LSB)     | 1          |
| 1           | 2          |
| 2           | 3          |
| 3           | 5          |
| 4 (MSB)     | 6          |

Byte 7 bits 0, 4, and 7 are **always zero** and reserved.

Encoding formula:

```
byte7 = ((counter & 0x07) << 1) | ((counter & 0x18) << 2)
```

Decoding formula (from byte 7 back to counter):

```
counter = ((byte7 >> 1) & 0x07) | ((byte7 >> 2) & 0x18)
```

This matches the pattern hardcoded in the bridge's `stario_proto.py`
and is now byte-for-byte verified.

## Observed progression

Sending 32 ETBs in sequence produced 33 ASB frames (one initial idle
frame at counter=0, then 32 incremented frames). All 32 distinct byte-7
values match the formula above:

| ETB# | byte 7 | binary    | counter | notes |
|------|--------|-----------|---------|-------|
| (init) | `0x00` | `0000_0000` | 0 | before any ETB |
| 1    | `0x02` | `0000_0010` | 1 | |
| 2    | `0x04` | `0000_0100` | 2 | |
| 3    | `0x06` | `0000_0110` | 3 | |
| 4    | `0x08` | `0000_1000` | 4 | |
| 5    | `0x0A` | `0000_1010` | 5 | |
| 6    | `0x0C` | `0000_1100` | 6 | |
| 7    | `0x0E` | `0000_1110` | 7 | |
| 8    | `0x20` | `0010_0000` | 8 | bit 5 of byte 7 turns on for counter ≥ 8 |
| 9    | `0x22` | `0010_0010` | 9 | |
| ...  | ...    | ...       | ...     | |
| 15   | `0x2E` | `0010_1110` | 15 | |
| 16   | `0x40` | `0100_0000` | 16 | bit 6 of byte 7 turns on; bit 5 toggles back off |
| 23   | `0x4E` | `0100_1110` | 23 | |
| 24   | `0x60` | `0110_0000` | 24 | both bits 5 and 6 set |
| 31   | `0x6E` | `0110_1110` | 31 | maximum value before wrap |
| 32 (= ETB#32 wraps) | `0x00` | `0000_0000` | 0 | counter rolled over after 32nd ETB |

The capture also exercised the wrap: after the 31st block produced
`0x6E`, the 32nd block produced `0x00` again — the counter is genuinely
modulo-32 with no separate "I wrapped" flag.

## Byte 2 bit 1 = "ETB executed"

A subtle but important observation comparing this capture to the
initial ASB frame:

- The first ASB frame (sent after `ENABLE_ASB + CLEAR_ETB`, before any
  ETB has been sent) has **byte 2 = `0x00`** — clean.
- Every subsequent ASB frame (response to an ETB) has
  **byte 2 = `0x02`** — bit 1 set.

So byte 2 bit 1 is the **"ETB just executed"** flag. The stario_proto.py
parser already labels this flag; this capture confirms when it sets and
clears.

The pre-existing print-simple capture's final ASB had byte 2 = `0x00`
because that probe drained for 5 seconds after the last ETB before
closing — long enough for the printer to clear the ETB-executed flag in
a subsequent (tracker-suppressed) ASB push. In etb-sweep there's no
gap, so every observed frame is "freshly post-ETB" and the flag is set.

## Files

- `capture.pcap` — full TCP/9100 capture (142 packets: handshake, 32
  blocks each producing TCP+ASB+ACK, cut, FIN)
- `capture.pcap.txt` — text dump
- `raw.bin` — concatenated 11-byte LAN ASB frames (363 bytes = 33 frames)
- `frames.log` — only first frame logged (per the LAN-decode bug
  documented in `../asb-states/notes.md`)
- `probe.log` — full event log including all 32 block-send timestamps
- `meta.txt`
