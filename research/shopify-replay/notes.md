# shopify-replay — real Shopify print job, captured + replayable

This is the highest-fidelity reference in the pack — what an actual
Shopify POS app on Android sends to a Star LAN printer, byte-for-byte.

## Files

- **`sample-shopify-receipt.bin`** (49165 bytes) — the verbatim TCP/9100
  payload Shopify sent. **This is the replay fixture.** It contains a
  full receipt encoded as StarPRNT-mode raster bitmap commands plus ETB
  checked-block markers and a partial-cut at the end.
- **`sample-9101-query.bin`** (51 bytes) — the status-poll payload
  Shopify sends each time it opens TCP/9101. Format: `0x32` followed by
  50 zero bytes.
- `main-net.pcap` — full capture from the main-net side: discovery
  exchange, the print connection on 9100, and 60 short-lived 9101
  status polls.
- `printer-net.pcap` — same exchange after the VM's NAT, on the
  isolated wire to the real printer. Useful for seeing the printer's
  ASB frames in response to Shopify's ETBs.
- `discovery-emulator.log` — the bridge emulator's own log showing how
  Shopify's discovery progressed (5+ STR_BCAST queries during the print
  flow, all answered).

## Replay

```sh
python3 research/probe.py replay-shopify <printer-ip> --port 9100 \
    --out /tmp/replay --hold 8 \
    --fixture research/shopify-replay/sample-shopify-receipt.bin
```

The probe streams the fixture in 4 KB chunks, draining ASB frames between
bursts so the response timing is captured. Default `--fixture` already
points at this file, so the command shortens to:

```sh
python3 research/probe.py replay-shopify <printer-ip> --out /tmp/replay
```

## Key findings from this capture

### 1. Shopify uses StarPRNT raster mode, not text ESC/POS

The 49 KB payload contains **54 occurrences of `ESC * r`** — StarPRNT
raster image commands. Only one `ESC RS E 0` (clear ETB) and zero
text-mode formatting commands (`ESC !`, `ESC E`, etc.). Shopify renders
the entire receipt — including text — as a bitmap on the client side
and sends it to the printer as raster. This is why receipts look
identical regardless of printer-side font config: Shopify never relies
on printer text mode.

### 2. Shopify uses ETB checked blocks aggressively

Six `ETB` (`0x17`) markers in the 49 KB stream. Each marker is the
StarPRNT "checked-block boundary" — the printer ACKs each block with
an 11-byte LAN-format ASB frame, letting the client confirm receipt
before sending more. This is exactly the protocol the bridge's
`stario_proto.py` decodes.

### 3. 9101 status polling pattern

Real Shopify on 9101 differs from our `9101-lifecycle/` capture (which
just opened-and-idled). Real flow per poll:

```
SYN -> SYN/ACK -> ACK
phone -> printer:  51 bytes  (0x32 + 50 × 0x00 — StarPRNT status request)
printer -> phone:  22 bytes  (FIN/PSH/ACK with 22-byte payload — 2× ASB frames concatenated)
phone -> printer:  FIN/ACK
printer -> phone:  ACK
```

The 22 bytes the printer sends back is **two consecutive 11-byte ASB
frames** — current state and immediately-prior-state, providing a
short history. Total wall-clock time per poll: under 50ms.

Shopify polls 9101 **continuously** while a printer is added, pre-
print, during print, and post-print. We saw 60 separate 9101 polls in
the 90-second capture window, which works out to roughly one every 1.5
seconds on average (with bursts). The bridge needs to handle this poll
volume cleanly — stale-connection accumulation here is what motivated
`print_server_bidir.py`'s "accept+FIN" design (commits `756714b`,
`bfb79b2`).

### 4. Discovery is repeatedly re-queried

The bridge emulator's log shows the phone sent **5 separate
STR_BCAST queries** during the 90-second window. Even after Shopify
"knows" the printer's IP, it re-discovers periodically — perhaps to
verify the printer is still online, perhaps as a freshness check
before each print job. The emulator must respond reliably on every
query; a missed response could kick the printer out of Shopify's
"online" list.

### 5. The 51-byte status query format

The full bytes:

```
32  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
```

Just `0x32` followed by 50 zero bytes. The leading `0x32` is the StarPRNT
"status request" marker; the trailing zeros are likely a length-padded
header. The bridge's `print_server_bidir.py` already handles this query
shape (responds with 2× 11-byte ASB frames then FIN), and this capture
confirms it byte-for-byte.

## Note on payload anonymization

`sample-shopify-receipt.bin` is *NOT* MAC/IP-rewritten because it's
application-layer data — TCP payload only, no IP/MAC fields embedded.
However, the bitmap content (a real Shopify test receipt) may still
include readable text (store name, order number, prices, etc.) baked
into the rendered raster. The receipt was generated from a test order
in a developer Shopify account, and the captured raster has been
inspected — no PII or sensitive business data is present. If your own
captures contain a real production receipt, decode the raster bytes
and verify before committing.
