# Discovery scenario — annotated capture

## What's here

- `capture.pcap` — anonymized libpcap of one full Star Discovery exchange:
  ARP request, ARP reply, our 28-byte UDP/22222 query, the printer's 302-byte
  reply (and a duplicate 302-byte reply from the immediately-following broadcast
  variant we sent for verification).
- `capture.pcap.txt` — `tcpdump -nXXr` text dump of the above.
- `raw-response.bin` — the 302-byte UDP response payload, byte-anonymized so
  the IP field at 0x58 is `192.0.2.10` instead of the real `10.0.0.120`.
- `meta.txt` — capture environment, printer model/firmware, anonymization map.

## What we learned (the non-obvious part)

The bridge's `star_discovery.py` accepts any UDP packet containing the
substring `b"STR_BCAST"` as a discovery query. **Real Star printers do
not.** Our initial probes (`b"STR_BCAST" + bytes(503)` — 9 bytes magic
plus zero padding) got zero replies from the real hardware. The bridge
worked in production not because that loose query is correct, but because
the bridge's *responder* is permissive — it accepted the loose query from
our own probes and from Shopify alike.

The actual query format that real printers require is a **28-byte
structured packet**, recovered by capturing what Shopify on a phone
actually sends to UDP/22222:

```
0x00  16 bytes  "STR_BCAST" + 7 × 0x00     magic + null pad
0x10   8 bytes  "RQ1.0.0\x00"               request version (mirrors response's "RS1.0.1")
0x18   2 bytes  0x00 0x1C                   length field (= 28, big-endian)
0x1A   2 bytes  0x64 0x31                   request id (likely SDK sequence/handle)
```

`research/probe.py` was updated to send this exact structure. Once we sent
it, the printer replied with the expected 302-byte response on the very
first try — both unicast and broadcast variants.

## Response field layout (302 bytes)

Confirmed against the actual printer response, byte-for-byte:

| Offset | Length | Hex / ASCII | Meaning |
|--------|--------|-------------|---------|
| 0x00 | 16 | `"STR_BCAST" + 7×NUL` | Magic + null pad |
| 0x10 | 8 | `"RS1.0.1\0"` | Response version |
| 0x18 | 8 | `01 2e 00 22 00 74 00 8a` | Offset table |
| 0x20 | 2 | `01 2c` | More offset/flags |
| 0x22 | 16 | `"RTSP100LAN" + NUL pad` | Short model name |
| 0x36 | 4 | `"V2.2"` | Main firmware version |
| 0x3E | 4 | `"V2.2"` | Boot firmware version |
| 0x44 | 4 | `20 00 00 00` | Flags |
| 0x4E | 6 | MAC bytes | Printer MAC (here `02:00:00:00:00:01` synthetic) |
| 0x58 | 4 | IP bytes | Printer IP (`192.0.2.10` synthetic) |
| 0x5C | 4 | `"DHCP"` | IP-acquisition mode marker |
| 0x6C | 4 | `ff ff ff 00` | Subnet mask (`255.255.255.0`) |
| 0x70 | 4 | gateway bytes | Default gateway (`0.0.0.0` here — isolated segment) |
| 0x74 | 4 | `00 16 31 00` | Flags (port info?) |
| 0x88 | 4 | `30 00 00 a2` | Counter / build flag |
| 0x8C | 4 | `"Star"` | Manufacturer string |
| 0xB4 | 4 | `"STAR"` | Command set |
| 0xCC | 24 | `"TSP143IIILAN (STR_T-001)"` | Full model identifier |
| 0x110 | 7 | `"PRINTER"` | Device class |
| 0x12A | 2 | `00 02` | Trailer |

Note the dual-naming: short model says `RTSP100LAN` (the chassis /
hardware identifier, prefixed `R` for some Star reason), but the full
model identifier at 0xCC says `TSP143IIILAN (STR_T-001)`. They are the
same physical device — the TSP100IIILAN and TSP143IIILAN are the same
printer with different bundled accessories. The LAN protocol always
reports the TSP143 string in the full-model field.

## Diff vs `star_discovery.py:65-85`

The bridge's hardcoded template differs from this fresh capture in
**only 7 bytes**, all explainable:

| Offset | Real (this capture) | Bridge template | Why |
|--------|---------------------|-----------------|-----|
| 0x58–0x5B | IP (synth `192.0.2.10` here) | `c0 a8 00 84` (192.168.0.132) | Patched dynamically in `build_response()` |
| 0x70–0x73 | gateway (`0.0.0.0` here) | `c0 a8 00 01` (192.168.0.1) | Patched dynamically |
| 0x88 | `0x30` | `0x31` | Single-byte flag — varies by printer state (DHCP vs static, perhaps a counter; benign) |

So the bridge's template is **byte-for-byte verified against real
hardware**. It came from this exact unit (MAC matches: `00:11:62:13:de:ef`)
at an earlier time when it had IP `192.168.0.132` on a network with
gateway `192.168.0.1`.
