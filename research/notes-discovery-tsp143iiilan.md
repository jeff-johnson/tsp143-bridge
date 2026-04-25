# Star Discovery Protocol — TSP143IIILAN reference

This is the cross-cutting analysis of the Star Discovery Protocol (SDP)
on UDP/22222, captured against a real Star Micronics TSP143IIILAN
(chassis-stamped TSP100IIILAN, MAC `00:11:62:13:de:ef`, firmware V2.2
boot + V2.2 main). It supplements the byte-level dump in
`research/discovery/notes.md` and the pcap at
`research/discovery/capture.pcap`.

If you found this doc because LAN discovery isn't working with a real
Star printer, **start with the "Real-printer query format" section
below** — that's where most reverse-engineering attempts dead-end.

## The protocol in one paragraph

A client sends a UDP packet to port 22222 (subnet broadcast or unicast,
both work). If the packet matches the expected **28-byte structured
query** described below, the printer replies with a **302-byte
fixed-layout response** whose offsets carry the printer's MAC, current
IP, subnet mask, gateway, model name, firmware version, manufacturer,
and a few flags. The Star SDK (StarIO / StarPRNT) parses that response
into a `StarPrinter` object and the discovery is done. There is no
multi-step handshake, no authentication, no encryption.

## Real-printer query format (the trap)

The bridge's own responder in `../star_discovery.py` accepts any UDP
packet that *contains* the substring `b"STR_BCAST"` anywhere in its
payload. **Real printers do not.** A loose `b"STR_BCAST" + bytes(503)`
gets dropped silently. We confirmed this experimentally against hardware
on an isolated wired segment with no firewalls, no NAT, no routers in
the path — the printer simply ignored the loose query.

The query Shopify POS actually sends is exactly **28 bytes**:

```
offset  length  bytes                          meaning
------  ------  -----------------------------  ------------------------
0x00    16      "STR_BCAST" + 7 × 0x00         magic + null pad
0x10     8      "RQ1.0.0" + 1 × 0x00           request version
0x18     2      0x00 0x1C                      length field (= 28, big-endian)
0x1A     2      0x64 0x31                      request id (likely SDK sequence/handle)
```

Field details:

- **Magic** is `STR_BCAST` ASCII null-padded to a 16-byte cell. Same
  byte sequence the response begins with, so a stateless responder can
  echo it back.
- **`RQ1.0.0`** is the request-version mirror of the response's
  `RS1.0.1` (offset 0x10). Both end in a single null. Differ in
  R**Q**/R**S** byte and in the third version digit (`0` vs `1`); we
  did not investigate whether earlier `RQ` versions are also accepted.
- **Length** is 16-bit big-endian and equals the total payload length
  (28). On the response, the equivalent field at offset 0x18 is `0x012e`
  (= 302). Set this correctly on the request — it likely passes a
  sanity check before the magic does.
- **Request id** appears to be an arbitrary 16-bit value picked by the
  client. We observed `0x6431` from Shopify on Android. The printer
  does not echo it back in the response (no field correlates), so this
  may be ignored on the wire and used purely client-side for matching
  responses to outstanding requests in the SDK.

We have not exhaustively fuzzed which fields are strictly required vs.
loosely tolerated, only that this exact 28-byte query gets a reply 100%
of the time and the loose form gets nothing.

## Response format (302 bytes)

See `research/discovery/notes.md` for the full annotated dump. The
high-level layout:

| Offset | Length | Field |
|--------|--------|-------|
| 0x00 | 16 | Magic + null pad — `"STR_BCAST"` |
| 0x10 | 8 | Response version — `"RS1.0.1\0"` |
| 0x18 | 2 | Total length — `0x012E` (= 302) |
| 0x22 | 16 | Short model name — e.g. `"RTSP100LAN"` |
| 0x36 | 4 | Firmware version — `"V2.2"` |
| 0x3E | 4 | Boot version |
| 0x4E | 6 | Printer MAC |
| 0x58 | 4 | Printer IP |
| 0x5C | 4 | IP-acquisition mode — literal ASCII `"DHCP"` or `"STAT"` |
| 0x6C | 4 | Subnet mask |
| 0x70 | 4 | Default gateway |
| 0x88 | 4 | Counter / state flag (varies between captures) |
| 0x8C | 4 | Manufacturer string — `"Star"` |
| 0xB4 | 4 | Command set — `"STAR"` |
| 0xCC | 24 | Full model identifier — `"TSP143IIILAN (STR_T-001)"` |
| 0x110 | 7 | Device class — `"PRINTER"` |
| 0x12A | 2 | Trailer — `0x00 0x02` |

Most other bytes are static across captures; the bridge's hardcoded
template at `../star_discovery.py:65-85` is verified byte-for-byte
correct except for those dynamic IP / gateway / counter fields.

## Where we found the query format

The 28-byte query format is not, as far as we can tell, in any of Star's
public documentation. We recovered it by:

1. Running `tcpdump` on a Mac with a Shopify POS phone on the same WiFi
2. Triggering "Add printer" in Shopify on the phone (without manually
   entering an IP)
3. Extracting the UDP/22222 payload from the resulting pcap
4. Replaying those exact bytes against the real printer, which then
   replied where every prior probe attempt had failed

The capture from step 1 shows two identical 28-byte STR_BCAST broadcasts
roughly two seconds apart, both with request id `0x6431`. (See
`research/raw-unscrubbed/discovery/capture.pcap` if you want to verify;
it's anonymized and committed.)

## What "doesn't work" looks like — known dead ends

For the next person reverse-engineering this:

1. **Sending plain `b"STR_BCAST"` plus padding gets no reply.** Real
   printers reject. The bridge accepts because of its substring match.
2. **Sending only the 16-byte magic cell** also gets no reply. The
   length and request-version fields appear required.
3. **Sending to TCP/22222** is *not* discovery. TCP/22222 is the
   "NIC reset" / configuration channel and accepts a different message
   type entirely. Confirmed open on this firmware but does not echo
   anything to a `STR_BCAST` payload.
4. **Sending UDP/161 (SNMP) or UDP/3289 (Epson ENPC)** — silent. This
   firmware has SNMP compiled out (or the listener is dead in V2.2);
   ENPC is Epson-specific and never expected to work on a Star printer.
5. **Sending mDNS service-type queries** — silent. This printer
   advertises *no* mDNS services. (Star printers in the TSP143IV+
   product line use mDNS; the TSP100IIILAN / TSP143IIILAN at firmware
   V2.2 does not.)
6. **Toggling `c_22222` ("NIC Reset Control") via the web admin** — has
   no effect on UDP/22222 discovery. The visible toggle on this
   firmware build is wrapped in an HTML comment behind
   `<!-- #if PORT22222_MOUNT ... -->` and only governs the TCP/22222
   reset behavior anyway. POSTing `c_22222=Enable` to `nsys_cgi`
   followed by `save_cgi` (with `radio_sv=r_save_res`) reboots the
   printer cleanly but does not bring up the UDP listener. The label
   is misleading — it does not enable LAN discovery.
7. **Toggling firewall / NAT settings** is irrelevant. The printer
   responds to discovery on the same physical broadcast domain as the
   client, regardless of firewall posture, *if* the query is well-formed.

## Network-topology gotchas

The protocol is broadcast-based, so usual rules apply:

- **Same L2 segment required.** Routers do not forward UDP/22222 by
  default. If the printer is wired and the client is on WiFi, the AP
  must bridge broadcasts cleanly (most do; some mesh systems with
  client-isolation features do not).
- **NAT'd setups need explicit UDP forwarding.** If the printer sits
  behind a NAT gateway (as in our deployed Pi setup with iptables DNAT
  on TCP 9100/9101/22222/80), UDP/22222 is *not* automatically forwarded.
  Discovery from the WAN side will fail; only TCP traffic gets
  port-forwarded. This is by design — Star expects discovery to happen
  on the same broadcast domain as the printer.
- **Replies are unicast to the source IP/port.** A capture host that is
  not the discoverer (e.g. tcpdumping on a third Mac while the phone
  does discovery) will see the broadcast request but **not** the
  printer's unicast reply, because WiFi APs do not flood unicast frames
  to non-destination clients. To capture both directions reliably, run
  the discovery from the same host that's running tcpdump.

## How to reproduce this capture

The wired-VM setup we used:

- A Debian VM with two NICs: one on the main LAN, one wired directly to
  the printer via a separate switch port (10.0.0.0/24 segment).
- `dnsmasq` running on the printer-segment NIC, serving DHCP from
  10.0.0.100–10.0.0.200.
- Python script sending the 28-byte structured query to the printer's
  DHCP-assigned IP, plus a tcpdump capturing both directions.

`research/probe.py discovery` now sends the correct 28-byte query, so
the standard `research/capture.sh ... --scenario discovery` will Just
Work against any real Star printer on the same L2 segment as the
capture host. The bridge will also still respond, since its substring
match is permissive.
