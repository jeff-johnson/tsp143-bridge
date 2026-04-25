# print-simple — minimal ESC/POS print job over TCP/9100

## What's here

- `capture.pcap` — 20-packet anonymized pcap of one full print job
- `capture.pcap.txt` — `tcpdump -nXXr` text dump
- `probe.log` — high-level events (connect, tx, end)
- `frames.log` — decoded ASB frames the printer sent back
- `raw.bin` — raw bytes received from the printer (33 bytes; 3× 11-byte ASB)
- `meta.txt`

## Sequence

The probe (see `../probe.py mode_simple`) does:

1. TCP/9100 handshake to the printer
2. `ENABLE_ASB` (`ESC RS a 1` — 4 bytes: `1b 1e 61 01`)
3. Wait 0.5s, drain any unsolicited ASB → printer sends one 11-byte ASB
4. `ESC @` + body + `ETB` (32 bytes total — block 1 with checked-block marker)
5. Wait 1s → printer sends another 11-byte ASB after ETB
6. block 2 + `ETB` (17 bytes) → another 11-byte ASB
7. `\n\n\n` + `GS V 1` (partial cut, 6 bytes)
8. Hold 5s, drain any final ASB
9. Close connection (FIN/ACK both ways)

## What the wire looks like

| Time (s) | Direction | Bytes | Notes |
|----------|-----------|-------|-------|
| 0.000 | host→printer | SYN | TCP handshake |
| 0.001 | printer→host | SYN/ACK | |
| 0.002 | host→printer | 4 | `ENABLE_ASB` |
| 0.004 | printer→host | 11 | first ASB frame (clean state) |
| 0.507 | host→printer | 32 | block 1 + ETB |
| 0.520 | printer→host | 11 | ASB after ETB ack |
| 1.525 | host→printer | 17 | block 2 + ETB |
| 1.530 | printer→host | 11 | ASB after ETB ack |
| 2.534 | host→printer | 6 | partial cut (`\n\n\n GS V 1`) |
| 7.558 | host→printer | FIN | close |

The printer's TCP receive window starts at 7300 bytes and stays there
throughout — the printer never closes the window during a small job
(no flow control needed at this scale; for raster-heavy jobs see
`../shopify-replay/notes.md`, which contains a 49 KB Shopify-rendered
raster receipt).

## ASB frame observed

All three ASB frames returned during this clean print were identical:

```
23 86 00 00 00 00 00 00 00
   ^^
   version byte 0x86 — bit 7 set = LAN format
                       (USB ASB uses 0x06 here)
```

The 11-byte LAN format pads two extra zero bytes on the end vs the USB
9-byte form. See main `../README.md` ASB layout table for byte-by-byte
field meanings. Decoded as `v3 sz=9 etb=0 clean` by `stario_proto.py`.

## Comparison with the bridge's emulation

`print_server_bidir.py` does the USB-to-LAN ASB transformation (see
`usb_asb_to_lan_format()` — sets bit 7 of byte 1, pads to 11 bytes).
This capture confirms the LAN-side wire format the transformation
targets, byte-for-byte.

