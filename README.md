# TSP143IIIU Wi-Fi Print Bridge

Turn a ~$15 Raspberry Pi Zero 2 W into a Wi-Fi print server for the Star
Micronics **TSP143IIIU** (USB-only) receipt printer so the Shopify POS app
can print to it wirelessly — including cash-drawer kick and real printer
status — with no CUPS, no Star driver, and no paid software.

The bridge impersonates a Star LAN printer (TSP143IIILAN) closely enough that
Shopify's StarIO SDK discovers it, prints to it, fires the cash drawer, and
reads real-time printer status with no errors or duplicate prints.

This repo also ships the **most complete public reverse-engineering of
the Star Discovery Protocol, StarPRNT ASB framing, and the StarPRNT
TCP/9101 status-poll wire format** that we're aware of, captured byte-
for-byte from a real TSP143IIILAN. See [Protocol research](#protocol-research)
below.

## Contents

- [How it works](#how-it-works) — problem, solution, architecture, data flow
- [Star ASB protocol](#star-asb-protocol) — what ASB is, USB vs LAN frame
  differences, the ETB counter, ports 9100 and 9101
- [Price comparison](#price-comparison)
- [Requirements](#requirements)
- [Installation](#installation)
- [Shopify POS setup](#shopify-pos-setup)
- [File reference](#file-reference)
- [Diagnostics](#diagnostics)
- [Operation](#operation)
- [Troubleshooting](#troubleshooting)
- [Adapting to other printers](#adapting-to-other-printers)
- [Protocol research](#protocol-research) — pcaps + analysis docs from a
  real Star LAN printer
- [Search index for reverse-engineers](#search-index) — keywords this repo
  intentionally surfaces
- [License](#license)

## How it works

### The problem

The TSP143IIIU is USB-only. An iPad can't connect to it directly (the Star
printer stand passes power only, not data). The printer needs to reach the
network somehow, and Shopify POS specifically requires the StarIO protocol —
AirPrint, IPP, and generic network printing won't work.

### The solution

This bridge sits between the iPad and the USB printer, speaking StarIO over
TCP on the network side and raw USB on the printer side. It presents itself
as a native Star LAN printer so Shopify POS discovers and uses it
automatically.

### Architecture

The bridge exposes two TCP ports plus a UDP discovery responder, exactly
like a real Star LAN printer:

| Port | Protocol | Purpose |
|------|----------|---------|
| **9100** | StarIO print (TCP) | Bidirectional data channel. Shopify sends StarPRNT raster print data; the printer sends back ASB (Automatic Status Back) frames reporting cover, paper, error, and job-completion status. |
| **9101** | StarIO status (TCP) | Short-lived status polling channel. Shopify connects, sends a 51-byte query (`0x32` + 50 NULs), receives two 11-byte ASB frames (22 bytes total), then disconnects. Polled multiple times per second during printing. |
| **22222** | Star Discovery Protocol (UDP) | Responds to the 28-byte structured `STR_BCAST` query so Shopify auto-discovers the printer. The reply is a fixed-layout 302-byte StarPRNT identity packet (model, IP, MAC, mask, gateway, firmware version). |

A single-process `select()` event loop multiplexes all TCP connections and
the USB device (`/dev/usb/lp0`) with non-blocking I/O. A separate lightweight
process (`star_discovery.py`) handles UDP discovery.

### Data flow

```
Shopify POS (iPad)                       Bridge                      USB Printer
──────────────────                       ──────                      ───────────

1. UDP broadcast 28-byte STR_BCAST  ───> star_discovery.py
   <── 302-byte discovery response ─────  (model, IP, port, MAC, mask, gw)

2. TCP connect to 9101 ──────────────────>
   Send 51-byte status query ───────────>
   <── 2x 11-byte ASB frames (22 bytes)── (cached from last USB read)
   <── TCP FIN ──────────────────────────

3. TCP connect to 9100 ──────────────────>
   Send ESC RS E 0 (clear ETB counter) ─> forward ──────────────────> /dev/usb/lp0
   Send ETB marker ─────────────────────> forward ──────────────────>
                                                  <── ASB (9 bytes) ──
   <── ASB (11 bytes, LAN format) ─────── transform & forward

4. Send raster print data (4-50 KB) ───> forward ──────────────────>
   (Shopify polls 9101 every ~220ms     (ASB cached, served on 9101)
    to watch ETB counter change)

5. Printer finishes, sends final ASB ──────────── <── ASB ──────────
   <── ASB (job complete) ──────────────── forward
   TCP FIN (clean disconnect = success)
```

## Star ASB protocol

### What is ASB?

ASB (Automatic Status Back) is Star's bidirectional status protocol. The
printer sends fixed-length status frames reporting its internal state:

| Byte | Contents |
|------|----------|
| 0 | Header — encodes frame length in bits 1-3 and 5-6. Bit 0 is always 1 (frame marker). |
| 1 | Version — protocol version in bits 1-3 and 5-6. **Bit 7 is set (`0x80`) on LAN printers** (`0x86` vs `0x06` for USB). |
| 2 | Printer status 1 — ETB executed (bit 1), cover open (bit 5), offline (bit 3). |
| 3 | Printer status 2 — mechanical error (bit 2), cutter error (bit 3), head overheat (bit 6). |
| 4 | Printer status 3 — head up (bit 1), paper jam (bit 2), command error (bit 5). |
| 5 | Sensor info — paper near end (bits 1-2), paper out (bit 3). |
| 6 | Sensor info 2 — presenter paper (bit 1), slip detectors (bits 2-3). |
| 7 | ETB counter — 5-bit counter scattered across bits 1, 2, 3, 5, 6 (non-contiguous). |
| 8 | Reserved (zero). |
| 9-10 | Additional bytes on LAN printers (zero-padded). |

For the byte-level decoded version verified against real hardware, see
[`research/notes-asb-states.md`](research/notes-asb-states.md).

### USB vs LAN frame differences

The USB printer (TSP143IIIU) sends **9-byte** frames with byte 1 = `0x06`.
The LAN printer (TSP100IIILAN / TSP143IIILAN) sends **11-byte** frames with
byte 1 = `0x86`. The bridge transforms USB frames to LAN format by:

1. Setting bit 7 of byte 1 (LAN interface flag)
2. Padding to 11 bytes with zeros

This is necessary because StarIO clients parse the version byte to determine
the printer type and expected frame size.

### ETB counter — the key to avoiding duplicate prints

The ETB (End-of-Transmission-Block, `0x17`) counter is the most critical
field in the ASB frame. Here's how it works:

1. Shopify sends `ESC RS E 0` to **reset the counter to zero**
2. Shopify sends print data containing **ETB markers** at block boundaries
3. The printer increments its internal counter each time it processes an ETB
4. Shopify **polls port 9101** and reads the counter from the ASB
5. When the counter stops changing, Shopify knows the job is complete
6. After the printer physically finishes (cuts paper), it sends one final
   unsolicited ASB with the counter incremented once more

The counter is 5 bits wide (range 0–31) and **wraps to 0 after 31** with no
separate wrap flag. The 5 bits are scattered through byte 7 in
non-contiguous positions — see
[`research/notes-asb-states.md`](research/notes-asb-states.md) for the
exact bit-position mapping (byte-for-byte verified across 32 sequential
ETBs in [`research/etb-sweep/`](research/etb-sweep/)).

**Critical design lesson:** An earlier version of this bridge generated
"synthetic" ASB responses immediately when it saw ETB markers in the data
stream, without waiting for the real printer. This created two independent
counters — the bridge's software counter (served on 9100 and 9101) and the
real printer's hardware counter (never seen by Shopify). The counters
inevitably desynchronized, causing Shopify to think every job failed and
retry — resulting in 3x duplicate prints.

**The correct approach:** Forward ALL data to the USB printer unmodified.
Let the printer process ETBs and maintain its own counter. Forward the
printer's real ASB frames back to the client. The printer is the single
source of truth for status. This is simpler and 100% correct.

### Port 9101 status polling

Shopify's StarIO SDK uses port 9101 as a rapid-polling status channel with
a specific protocol:

1. TCP connect
2. Send 51 bytes: `0x32` followed by 50 zero bytes
3. Receive 22 bytes: two consecutive 11-byte ASB frames
4. Server closes connection (FIN)

This cycle repeats many times per second during active printing. Between
print jobs, Shopify makes 2-3 probe-and-disconnect cycles before committing
to send data — this is normal behavior observed on both real LAN printers
and this bridge.

The **51-byte query format** and the **22-byte two-frame response shape** are
documented byte-for-byte in
[`research/notes-9101-lifecycle.md`](research/notes-9101-lifecycle.md).
A real-Shopify capture of the 51-byte query is committed at
[`research/shopify-replay/sample-9101-query.bin`](research/shopify-replay/sample-9101-query.bin)
for fixture-based testing.

The bridge serves the most recently cached ASB from the USB printer on 9101.
Since the cache updates whenever the real printer sends status, the 9101
responses always reflect the real ETB counter and printer state.

A real printer also has a deterministic **5-second idle timeout**: if a
client opens 9101 and sends nothing, the printer FIN's the connection
exactly 5 seconds later. The bridge's "accept+FIN" strategy (commit
`756714b`) was designed to match this; see
[`research/9101-lifecycle/`](research/9101-lifecycle/).

### Port 9100 print channel

All data from the client passes through to USB with minimal filtering:

- **ESC RS a 1** (enable ASB) and **ESC RS a 0** (disable ASB) are stripped
  because the bridge manages ASB on the USB side at startup and keeps it
  always-on.
- **Everything else** — including ETB markers, ESC RS E 0 (clear counter),
  ESC @ (reset), raster data, cash drawer commands — passes through
  unmodified to the USB printer.

ASB frames from the USB printer are transformed to LAN format and forwarded
back to the TCP client.

## Price comparison

| Model | New | Used / Refurb |
|---|---|---|
| TSP143IIIU (USB) | $280 - $355 | ~$180 |
| TSP143IIILAN (Ethernet) | $324 - $454 | $145 - $224 |
| TSP143IIIW (Wi-Fi) | ~$389 | -- |
| TSP143IIIBi (Bluetooth) | $375 - $519 | ~$290 |
| **Raspberry Pi Zero 2 W** | **$15** | -- |

If you already own a TSP143IIIU, adding a $15 Pi is roughly **20× cheaper**
than buying a networked Star printer.

*(Prices sampled April 2026 from POSSupply, Barcode Factory, eBay.)*

## Requirements

- Raspberry Pi Zero 2 W (or any Pi with Wi-Fi) on the same network as the iPad
- Star Micronics TSP143IIIU connected via USB, exposed as `/dev/usb/lp0`
- Python 3.9+ (ships with Raspberry Pi OS Lite)
- No CUPS, no Star CUPS driver, no Python dependencies beyond stdlib

## Installation

1. Flash Raspberry Pi OS Lite, configure Wi-Fi, SSH in.
2. Plug the TSP143IIIU into the Pi's USB port and power it on.
3. Clone and install:

```bash
git clone <repo-url> tsp143-bridge
cd tsp143-bridge
sudo ./install.sh
```

The installer:
- Installs Python 3 if missing
- Creates a `tsp-bridge` service user with `/dev/usb/lp0` access
- Copies runtime files to `/opt/tsp143-bridge/`
- Installs and enables the `tsp143-bridge` systemd service
- Prints the Pi's IP address for reference

## Shopify POS setup

1. On the iPad, open **Shopify POS > Settings > Hardware > Printers**.
2. The printer should appear automatically as a Star TSP143III.
3. For the cash drawer: **Settings > Hardware > Cash Drawer** > select the printer.

## File reference

| File | Purpose |
|------|---------|
| `print_server_bidir.py` | Main bridge — TCP 9100/9101 to USB bidirectional relay with ASB forwarding. Extensively commented for anyone adapting this to other Star printers. |
| `star_discovery.py` | UDP 22222 discovery responder — makes the bridge appear as a Star LAN printer on the network. Sends a real 302-byte StarPRNT identity reply. Reads live netmask + gateway via `SIOCGIFNETMASK` ioctl + `/proc/net/route`. |
| `stario_proto.py` | ASB frame parser and status decoder — handles both 9-byte USB and 11-byte LAN frame forms; decodes the 5-bit non-contiguous ETB counter. Shared between the bridge and diagnostic tools. |
| `stario_client.py` | Standalone StarIO diagnostic client — connect to the bridge or a real Star printer to inspect ASB frames, send test prints, and debug protocol issues. |
| `run.sh` | Launcher script — starts both the bridge and discovery responder, kills both if either exits. |
| `install.sh` | Installer — sets up systemd service, user, permissions. |
| `tsp143-bridge.service` | systemd unit file. |
| `asb.pdf` | Star's official ASB specification manual (reference). |
| `research/` | Packet captures and protocol notes from a real Star LAN printer. See [Protocol research](#protocol-research). |

## Diagnostics

`stario_client.py` speaks the same StarIO protocol as Shopify. Point it at
the bridge or a real Star LAN printer:

```bash
# Send a test print job and watch decoded ASB responses:
python3 stario_client.py <bridge-ip> --probe

# Passively watch ASB for 30 seconds (open/close cover, pull paper, etc.):
python3 stario_client.py <bridge-ip> --watch 30
```

For more sophisticated probing (full pcap captures, scenario walking,
Shopify-receipt replay) see [`research/probe.py`](research/probe.py) and
[`research/capture.sh`](research/capture.sh).

## Operation

```bash
# Live logs
journalctl -u tsp143-bridge -f

# Restart
sudo systemctl restart tsp143-bridge

# Status
systemctl status tsp143-bridge
```

CLI flags (edit `/opt/tsp143-bridge/run.sh` to customize):

- `print_server_bidir.py -p 9100 -s 9101 -d /dev/usb/lp0 -v`
- `star_discovery.py -m TSP143III -p 9100 -v`

## Troubleshooting

| Symptom | Cause & Fix |
|---------|-------------|
| Printer not discovered in Shopify | iPad and Pi must be on the same subnet with no client isolation. Check `journalctl -u tsp143-bridge -f` for `Discovery request from ...` lines. If the bridge logs show queries but Shopify doesn't list the printer, the iPad's WiFi may be on a separate VLAN — see also [`research/notes-discovery-tsp143iiilan.md`](research/notes-discovery-tsp143iiilan.md) for the full topology gotcha list. |
| Nothing prints | Verify `/dev/usb/lp0` exists (`ls -l /dev/usb/lp0`). Check the `tsp-bridge` user is in the `lp` group. Try `echo hello > /dev/usb/lp0` as root. |
| Duplicate prints | The bridge must forward ALL data to USB unmodified and forward ALL real ASB back. Any synthetic/cached ASB with a stale ETB counter will cause retries. |
| Cash drawer won't open | Check the RJ12 cable is in the printer's DK port. The drawer-kick command (`ESC p 0`) rides inline with print data — no special bridge handling needed. |
| Service won't start | `journalctl -u tsp143-bridge -e` — usually another process holds `/dev/usb/lp0` (stop CUPS: `sudo systemctl stop cups`). |

## Adapting to other printers

The core architecture (TCP-to-USB bridge with ASB forwarding) should work
with any Star Micronics printer that supports the StarLine ASB protocol.
Key things to adjust:

1. **Frame size:** Check your printer's ASB frame length (byte 0 encodes it).
   The `usb_asb_to_lan_format()` function pads to 11 bytes — adjust if your
   target LAN model uses a different size.

2. **Version byte:** LAN printers set bit 7 of byte 1. Check what your
   target model sends and match it.

3. **Discovery response:** The 302-byte discovery packet template in
   `star_discovery.py` was captured from a TSP100IIILAN. The chassis
   actually advertises itself as `TSP143IIILAN (STR_T-001)` in the
   full-model field — same hardware, two different model strings,
   depending on offset. For other Star LAN models, capture a real
   response with [`research/probe.py discovery`](research/probe.py) and
   diff it against the existing template.

4. **Port 9101 behavior:** The 51-byte query / 22-byte response pattern was
   captured from a TSP100IIILAN. Other Star LAN models likely use the same
   protocol, but verify with a packet capture if you have access to the real
   hardware.

5. **Never generate synthetic ASB.** Let the real printer handle all status.
   This is the single most important lesson from building this bridge.

## Protocol research

The [`research/`](research/) directory is the authoritative protocol-level
reference for this project. Every claim in this README about wire formats
is verified against captures from a **real Star Micronics TSP143IIILAN**
(chassis-stamped TSP100IIILAN, MAC `00:11:62:13:de:ef`, firmware V2.2).

Most of this is, to our knowledge, **not publicly documented anywhere
else**: Star Micronics' StarPRNT SDK ships compiled binary frameworks; the
official manuals describe ASB at a high level but do not specify the
discovery wire format, the ETB-counter encoding, the 9101 query/response
shape, or the difference between USB and LAN frame layouts. We worked it
out by capturing real traffic and replaying it.

### Cross-cutting analysis docs

These three documents are the synthesized references — read these first
if you're reverse-engineering:

- **[`research/notes-discovery-tsp143iiilan.md`](research/notes-discovery-tsp143iiilan.md)**
  — The Star Discovery Protocol on UDP/22222: the 28-byte structured
  query format Shopify actually sends, the 302-byte response layout, all
  the dead ends we ruled out (mDNS, SNMP, loose `STR_BCAST`), and the
  topology constraints that determine whether discovery works.
- **[`research/notes-asb-states.md`](research/notes-asb-states.md)** —
  StarPRNT ASB framing on LAN, byte-by-byte. The 5-bit non-contiguous
  ETB-counter encoding fully decoded with encode/decode formulas.
  Distinguishes USB and LAN forms.
- **[`research/notes-9101-lifecycle.md`](research/notes-9101-lifecycle.md)**
  — TCP/9101 status-port behavior including the 51-byte StarPRNT query
  format and the 22-byte two-frame response shape. Documents real
  Shopify polling cadence captured from an Android phone.

### Captured scenarios

Each scenario directory contains an anonymized `capture.pcap`, a
`capture.pcap.txt` text dump, a `notes.md` with annotated observations,
and a `meta.txt` with capture environment and substitution map.

| Scenario | What it captures |
|----------|------------------|
| [`research/discovery/`](research/discovery/) | Star Discovery Protocol query+response. Includes the 302-byte raw response binary. |
| [`research/print-simple/`](research/print-simple/) | Minimal ESC/POS print job with ASB enabled. |
| [`research/9101-lifecycle/`](research/9101-lifecycle/) | TCP/9101 idle test. Proves the 5.0s idle timeout. |
| [`research/shopify-replay/`](research/shopify-replay/) | A real Shopify POS print captured from an Android phone. Includes a 49 KB receipt fixture (`sample-shopify-receipt.bin`) and the 51-byte 9101 query (`sample-9101-query.bin`) ready for replay. |
| [`research/etb-sweep/`](research/etb-sweep/) | 32 sequential ETB checked blocks. Exercises the ETB counter through `0→31→0` wrap. |
| [`research/asb-states/`](research/asb-states/) | ASB byte layouts under cycled physical states (cover open/closed, paper out/loaded). |
| [`research/portscan/`](research/portscan/) | nmap full TCP + UDP top-50 fingerprint. Reveals an undocumented Telnet diagnostic utility on TCP/23. |

### Reusable tools and fixtures

- [`research/probe.py`](research/probe.py) — pure-stdlib active prober.
  Modes: `discovery` (sends the correct 28-byte structured query),
  `simple`, `drawer`, `etb-sweep`, `hold-9101`, `watch-asb`,
  `replay-shopify`, `raster`. Each mode writes raw socket traffic and
  decoded ASB frames alongside.
- [`research/anonymize.py`](research/anonymize.py) — pure-stdlib pcap
  rewriter. Substitutes printer/host MACs and IPs with RFC 5737
  documentation values and recomputes IPv4/UDP/TCP checksums.
- [`research/capture.sh`](research/capture.sh) — scenario driver that
  walks named scenarios end-to-end (tcpdump + active probe + anonymize
  + write companion text files).
- [`research/shopify-replay/sample-shopify-receipt.bin`](research/shopify-replay/sample-shopify-receipt.bin)
  — 49 KB binary, the verbatim TCP/9100 payload Shopify sent to print
  one receipt. Use as a replay fixture against any Star printer or the
  bridge.
- [`research/shopify-replay/sample-9101-query.bin`](research/shopify-replay/sample-9101-query.bin)
  — 51 bytes, the verbatim StarPRNT status query Shopify sends.

## Search index

If you found this repo searching for any of these terms, the relevant
content is here:

- **Star Discovery Protocol** / **SDP** / **STR_BCAST format** /
  **UDP 22222 query packet**: see
  [`research/notes-discovery-tsp143iiilan.md`](research/notes-discovery-tsp143iiilan.md).
  The exact 28-byte structured query (16-byte `STR_BCAST` magic +
  `RQ1.0.0` request version + `0x001C` length + 16-bit request id) is
  documented byte-for-byte. The 302-byte response layout with all
  offsets is also there.
- **StarPRNT SDK printer discovery** / **searchPrinter** /
  **SMPort.searchPrinter("TCP:")**: the SDK calls into a closed Star
  framework; this repo documents the wire-level result so you don't
  need the SDK.
- **TSP100IIILAN** / **TSP143IIILAN** / **TSP143III firmware V2.2**
  protocol behavior — see [`research/`](research/).
- **Shopify POS Star printer protocol reverse-engineering** /
  **Shopify POS port 9100 9101 capture** /
  **how does Shopify discover Star printer**: see
  [`research/shopify-replay/notes.md`](research/shopify-replay/notes.md)
  for an end-to-end real capture of an Android Shopify POS app
  printing a receipt, including the discovery, status polling, and
  print TCP streams.
- **StarPRNT ASB byte format** / **Automatic Status Back frame layout** /
  **ASB version byte 0x06 vs 0x86** / **USB ASB vs LAN ASB**: see
  [`research/notes-asb-states.md`](research/notes-asb-states.md).
- **StarPRNT ETB counter** / **ETB checked block protocol** /
  **5-bit ETB counter encoding** / **byte 7 ETB bits**: same doc;
  the exact non-contiguous bit-position mapping is given with
  encode/decode formulas verified against 32 sequential ETBs.
- **Star printer port 9101 protocol** / **StarPRNT status query 51 bytes** /
  **0x32 status query**: see
  [`research/notes-9101-lifecycle.md`](research/notes-9101-lifecycle.md).
- **TSP100III Telnet utility** / **Star printer telnet port 23**: the
  TSP100IIILAN firmware exposes an undocumented Telnet diagnostic
  service on TCP/23 with a banner that reveals model and MAC; see
  [`research/portscan/notes.md`](research/portscan/notes.md).
- **Star printer "NIC Reset Control" / `c_22222` setting / `PORT22222_MOUNT`
  conditional** — covered in
  [`research/notes-discovery-tsp143iiilan.md`](research/notes-discovery-tsp143iiilan.md)
  under "Known dead ends" — that admin-page toggle does not enable
  discovery; it gates a separate TCP/22222 reset feature and is
  compile-time-conditioned out of some firmware builds anyway.
- **Shopify Star printer duplicate prints fix** / **synthetic ASB ETB
  desync** — see "ETB counter — the key to avoiding duplicate prints"
  above for the architectural cause and fix.

## License

MIT — see [LICENSE](LICENSE). © 2026 Senternal LLC,
[senternaltech.com](https://senternaltech.com).
