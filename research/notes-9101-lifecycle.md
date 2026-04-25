# TCP/9101 Status Port — TSP143IIILAN reference

This is the cross-cutting analysis of TCP port 9101's lifecycle and
protocol on a real Star TSP143IIILAN. It supplements the per-scenario
captures in `research/9101-lifecycle/` (an idle-only test) and
`research/shopify-replay/` (real Shopify polling pattern), and is the
canonical reference for the bridge's `print_server_bidir.py` 9101
emulation strategy (commits `756714b`, `bfb79b2`).

## Purpose of port 9101

TCP/9101 is the StarPRNT **status poll-respond channel**. Unlike port
9100 (which streams print jobs and pushes ASB unsolicited when ASB is
enabled), port 9101 is exclusively for short, transactional status
queries. It exists so a client can ask "what's the printer's current
state?" without disturbing whatever is on 9100 — useful for status
polling alongside an active print, or when ASB hasn't been enabled.

## Connection lifecycle (real printer)

A typical real-Star 9101 connection lifetime:

```
T+0.000  client  -> printer  SYN                  win=65535
T+0.001  printer -> client   SYN/ACK              win=1460
T+0.002  client  -> printer  ACK
T+0.003  client  -> printer  PSH/ACK   51 bytes   (status query)
T+0.004  printer -> client   FIN/PSH/ACK 22 bytes (2× ASB frames)
T+0.005  client  -> printer  ACK
T+0.006  client  -> printer  FIN/ACK
T+0.007  printer -> client   ACK
```

Total wall time per poll: under 50 ms on a wired LAN.

If the client opens a connection and **sends nothing** (the case we
captured in `research/9101-lifecycle/`), the printer holds the socket
for **exactly 5.0 seconds** and then sends FIN. The 5-second idle
timeout is deterministic.

## TCP receive window — a clue about intent

The printer advertises a **1460-byte receive window** in its SYN/ACK on
port 9101, vs **7300 bytes** on port 9100:

| Port | RX window | Implication |
|------|-----------|-------------|
| 9100 | 7300 B | Designed for streaming bulk print data |
| 9101 | 1460 B | Designed for tiny short-lived requests |

`1460` is exactly one TCP MSS — the printer effectively says "I'll
accept one segment of payload, no more." If a client tries to send
more than ~1.5 KB of status-query bytes, it will block. This is fine
for the actual protocol, where queries are 51 bytes.

## The 51-byte status query

Recovered from the `research/shopify-replay/` capture; see also
`research/shopify-replay/sample-9101-query.bin`:

```
0x00  1 byte    0x32              query type marker (StarPRNT "status request")
0x01  50 bytes  0x00 × 50         zero-filled (header pad / reserved)
```

So a query is `0x32` followed by 50 NUL bytes, total 51 bytes. The
leading `0x32` is what `print_server_bidir.py` matches on as the "9101
query marker" (see comments in that file).

The trailing 50 bytes appear to be reserved/padding — sending arbitrary
non-zero bytes there has not been tested, but the bridge code does not
inspect them.

## The 22-byte response

The printer answers with **two consecutive 11-byte LAN ASB frames**,
sent as a single TCP `PSH/FIN/ACK` segment of length 22.

- The first frame is the **current state** (instantaneous snapshot at
  query time).
- The second frame is the **immediately-prior state**, providing
  one-step history.

If the printer state is steady, both frames are identical. If the state
just transitioned (for example, paper just ran out), the two frames
differ — current shows the new state, prior shows the previous one.

This 2-frame format is how the bridge's `print_server_bidir.py` answers
9101 polls: it caches the most recent ASB frame seen on the USB side
and serves it twice in the response.

The printer **always** closes the connection (sends FIN) immediately
after the 22-byte response. There is no second exchange on a single
9101 connection — one query, one response, then close.

## Real Shopify polling pattern

From the `research/shopify-replay/` capture (90-second window during a
real receipt print):

- **60 separate 9101 connections** initiated by the phone.
- Roughly one every 1.5 seconds on average, with bursts during/around
  print events.
- Each connection is a complete query+response cycle (the pattern
  above), then close. **Not a long-held connection.**
- Connections happen continuously: pre-print (printer added to the
  app), during print, and post-print. They do not stop after a job
  completes.

Implication for the bridge: stale-connection accumulation from this
poll volume is what motivated the bridge's "accept+FIN" strategy.
Holding 60+ open sockets per minute would exhaust file descriptors;
serving them as cheap accept-respond-close transactions matches the
real printer's behavior and is cheap to maintain.

## Implications for the bridge emulation

The bridge's `print_server_bidir.py` 9101 handler does the following:

1. Accept the TCP connection.
2. Read up to ~64 bytes (won't block on more — the query is 51 B).
3. If the first byte is `0x32`, treat as a valid query.
4. Send the cached most-recent ASB frame, doubled (22 bytes total).
5. Close (FIN).

This is byte-for-byte equivalent to the real printer's behavior with
two minor benignities:
- The bridge serves the **same** ASB twice rather than current+prior.
  Real Shopify does not appear to depend on the two frames differing.
- The bridge does not enforce a 1460-byte receive window — it'll accept
  whatever the kernel buffers. Real Shopify only ever sends 51 bytes,
  so this never matters.

## Edge cases observed and not observed

**Observed:**
- The printer FINs the connection *itself* on idle after 5 seconds. If
  the bridge follows suit, no client confusion arises.
- Real Shopify reliably closes its end after receiving the 22-byte
  response — so the bridge's FIN happens on a half-closed socket and
  the four-way teardown completes cleanly.

**Not observed in this work** (would benefit from future scenarios):
- Behavior of multiple parallel 9101 connections from the same client
  IP simultaneously (the printer's connection limit is undocumented).
- Whether the printer queues queries received during an active 9100
  print job, or services them concurrently (our captures suggest
  concurrent: ASB on 9100 and 9101 polls happen during the print
  with no observable interaction).
- Behavior when the query first byte is **not** `0x32` — the bridge
  ignores the connection entirely, but the real printer's response is
  unverified. Worth poking with a fuzzy probe in a follow-up scenario.

## Cross-references

- **Per-scenario captures:** `research/9101-lifecycle/notes.md` (idle
  test), `research/shopify-replay/notes.md` (real Shopify polling).
- **Fixture file:** `research/shopify-replay/sample-9101-query.bin` —
  the exact 51 bytes Shopify sends, ready for replay.
- **ASB byte layout** of the response frames is documented in
  `notes-asb-states.md`.
- **Bridge implementation:** `../print_server_bidir.py` — search for
  the comments around port 9101 and the "0x32 query marker"
  references.
