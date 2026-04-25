# asb-states — physical-state ASB byte layouts

What the printer pushes on TCP/9100 (with ASB enabled) when the operator
opens/closes the cover, pulls/reloads paper, and feeds.

## Captured states (all observed in this 90s window)

| # | hex (11-byte LAN ASB) | Decoded | Inferred state |
|---|------------------------|---------|----------------|
| 1 | `23 86 28 00 00 00 00 04 00 00 00` | byte2.bit5 + byte2.bit3 | cover open, no paper-sensor flag |
| 2 | `23 86 28 00 00 08 00 04 00 00 00` | byte2.bit5 + byte2.bit3 + byte5.bit3 | cover open + paper near end / out |
| 3 | `23 86 00 00 00 00 00 04 00 00 00` | all-zero status bytes | clean (cover closed, paper loaded, ready) |
| 4 | `23 86 08 00 00 08 00 04 00 00 00` | byte2.bit3 + byte5.bit3 | offline + paper sensor (transitional — paper being reloaded?) |

3 occurrences of patterns 1 and 2; 2 occurrences each of patterns 3 and 4.

## Byte-by-byte annotated layout (LAN format, 11 bytes)

```
offset  byte  meaning
------  ----  ------------------------------------------
0       0x23  Frame header — bits encode frame size (matches stario_proto.extract_frame())
1       0x86  Version. 0x86 = LAN format. (USB form uses 0x06.)
2       ----  Printer status 1
                bit 0  unused / clean
                bit 1  unused
                bit 2  unused
                bit 3  OFFLINE (set whenever printer can't print: cover open, paper out, error)
                bit 4  unused
                bit 5  COVER OPEN (lid raised over the paper roll)
                bit 6  unused
                bit 7  unused
3       0x00  Printer status 2 — mech error / cutter error / head overheat
              (unset throughout this capture — no mechanical errors)
4       0x00  Printer status 3 — head up / paper jam / cmd error
              (unset throughout — no jams, no command errors)
5       ----  Sensor 1 — paper sensors
                bit 3  PAPER NEAR-END or PAPER OUT (sensor triggered)
6       0x00  Sensor 2 — presenter / slip detectors (unset on this model)
7       0x04  ETB counter — does NOT change in watch-asb mode (no ETBs sent).
              Confirms the counter is server-driven, not periodic.
8-10    0x00  LAN-format zero pad (USB form has only 9 bytes; LAN appends 2)
```

## Observations

1. **`byte2.bit3 (offline)` is a derived flag.** It is set whenever any
   error condition holds: cover open, paper out, or a mechanical error.
   Cover-open frames have both `bit5` and `bit3` set; paper-sensor-only
   frames have `bit3` set without `bit5`.

2. **The same logical state can produce multiple ASB frames.** The
   printer pushed three "cover open + paper-sensor" frames during the
   capture, presumably as the cover-mounted sensors stabilized after
   the lid was lifted, then again when paper was being pulled/reloaded
   under the open cover.

3. **Pattern #4** (`byte2.bit3` set without `byte2.bit5`, with
   `byte5.bit3`) is the most interesting — it shows the printer
   transiently reports OFFLINE without COVER-OPEN. This is the
   **paper-out, cover-closed** state: paper was pulled while the cover
   was closed (or after pull+immediately-close), and the printer is
   reporting "I have no paper" without the cover being raised.

4. **No mechanical-error or head-up bits ever set.** Bytes 3 and 4
   stayed `0x00` throughout. Without inducing a paper jam or print-head
   fault we don't get those byte values exercised — would require
   either physical induction or a fault-injecting fixture.

5. **ETB counter stays constant at `0x04`.** With no ETB markers sent
   on the connection, the counter doesn't increment — confirming it
   tracks "ETBs received and processed" not wall-clock time. Pair this
   with `print-simple/notes.md`'s observation that the counter advances
   one per ETB during a print job to fully document its behavior.

## Caveat: probe.py / stario_proto.py LAN-frame decode bug

The `frames.log` here only records the FIRST ASB frame, because
`stario_proto.extract_frame()` is hardcoded for the 9-byte USB ASB
form. When given an 11-byte LAN frame it consumes only 9 bytes,
leaving the 2 trailing zero-pad bytes in the buffer; the next call
starts on `0x00 0x00 ...` which is not a valid frame header, so
extraction stops. The raw bytes still get written to `raw.bin`
(that happens before parsing), so no data is lost — but `frames.log`
under-counts.

**Workaround used to produce this scenario:** decoded `raw.bin`
manually in 11-byte chunks with a small inline Python script (see
the analysis under `research/asb-states/`).

**Suggested fix** (left for a future PR): teach `extract_frame()` to
recognize the LAN-form version byte (0x86) and consume 11 bytes
rather than 9 in that case.

## Files

- `capture.pcap` — full 9100 TCP capture (29 packets including SYN, ASB
  push frames, FIN)
- `capture.pcap.txt` — text dump
- `raw.bin` — concatenated 11-byte LAN ASB frames (110 bytes = 10 frames)
- `frames.log` — only the first frame, per the bug noted above
- `probe.log` — probe events (also single-line ASB)
- `meta.txt`
