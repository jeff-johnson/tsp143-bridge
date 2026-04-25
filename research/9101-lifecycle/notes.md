# 9101-lifecycle — status-port behavior over time

## What's here

- `capture.pcap` — 7-packet pcap of one full TCP/9101 connection lifecycle
- `capture.pcap.txt` — text dump
- `probe.log` — probe events
- `meta.txt`

`raw.bin` and `frames.log` are absent because **the printer sent zero
bytes during the entire connection**.

## The wire trace

```
T+0.000  host  -> printer  SYN
T+0.001  printer -> host   SYN/ACK    win=1460
T+0.002  host  -> printer  ACK
   ...    (5.0 seconds of mutual silence — neither side sends anything)
T+5.000  printer -> host   FIN/ACK    seq=1 ack=1   length=0
T+5.001  host  -> printer  ACK
T+5.002  host  -> printer  FIN/ACK
T+5.003  printer -> host   ACK
```

The printer's TCP receive window advertised at SYN/ACK is `1460` bytes,
which is ~8× smaller than the 9100 print port's `7300` window — a clear
signal that 9101 is for short status-poll exchanges, not bulk data.

## The 5-second idle timeout

The most important number from this capture is **5.0 seconds**. That is
the printer's deterministic close-on-idle timeout for the status port.

A LAN-aware client that wants printer status is expected to:

1. Open TCP/9101
2. Immediately send a status request (or just rely on ASB-push)
3. Read whatever the printer sends back
4. Close the connection

Holding the connection open without sending anything yields nothing. The
printer is not chatty here — port 9101 doesn't push unsolicited status
on idle connections; that role is played by **ASB on port 9100** when
ASB is enabled. Port 9101 is a poll-respond channel, not a streaming
push channel.

## How this informs the bridge

`../print_server_bidir.py`'s "9101 accept+FIN strategy" (see commits
`756714b`, `bfb79b2`) was designed empirically to match this behavior.
This capture confirms the design: when a client opens 9101 and the
bridge has nothing immediate to send, FIN'ing on a short timeout is the
correct emulation. The bridge's specific pattern — accept the
connection, send the cached ASB once, then FIN — is slightly more
generous than the real printer (which sends nothing on idle), but it's
within the protocol's tolerance.

## Follow-up captures worth doing

- **9101 with an active query.** What sequence does Shopify send during
  a real print, and what does the printer reply? This requires a real
  Shopify client; could be added when capturing a `shopify-replay`
  fixture.
- **9101 during a print job.** Open 9101 in a separate connection while
  9100 is mid-print. Does the printer push status updates over 9101?
  Or does ASB-push on 9100 remain the only push channel? Worth a
  scenario named `9101-during-print/`.
- **9101 with simulated error.** Open cover during the connection.
  Same question — does 9101 push the error, or only 9100/ASB?
