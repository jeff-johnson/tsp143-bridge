# Protocol research

This directory collects raw reference material captured from a real Star
Micronics LAN printer (TSP143IIILAN, chassis-stamped TSP100IIILAN, firmware
V2.2) and used while building and verifying the StarIO emulation in
`../print_server_bidir.py` and `../star_discovery.py`.

The bridge emulates what Shopify POS + the StarIO SDK expect a Star LAN
printer to do. Having honest captures of the real thing is the only way to
confirm that emulation is faithful, and the only way to diagnose regressions
without physical access to a LAN printer.

## What's captured

Each scenario lives under its own subdirectory and contains, at minimum,
`capture.pcap` (anonymized), `capture.pcap.txt` (text dump), `notes.md`
(per-scenario observations), and `meta.txt` (capture environment +
substitution map).

| Scenario | Contents |
|----------|----------|
| `discovery/` | Star Discovery Protocol (UDP/22222) request + 302-byte response |
| `print-simple/` | Minimal ESC/POS print job, ASB-enabled |
| `9101-lifecycle/` | Idle TCP/9101 connection — proves 5-second idle timeout |
| `shopify-replay/` | Real Shopify POS print job from an Android phone (full pcap + 49 KB receipt fixture for replay) |
| `etb-sweep/` | 32 sequential ETB checked blocks — proves the ETB counter encoding |
| `asb-states/` | ASB byte layouts under cycled physical states (cover open/closed, paper out/loaded) |
| `portscan/` | nmap full TCP + UDP top-50 service fingerprint |

Cross-cutting analysis docs (the deliverable for future devs):

- `notes-discovery-tsp143iiilan.md` — Star Discovery Protocol, including
  the 28-byte structured query format and full 302-byte response layout
- `notes-asb-states.md` — ASB framing on LAN, byte-by-byte, plus the
  5-bit non-contiguous ETB counter encoding fully decoded
- `notes-9101-lifecycle.md` — TCP/9101 status-port behavior, including
  the 51-byte query format and 22-byte two-frame response

## Toolchain

- `capture.sh` — script that walks scenarios end-to-end (prompts for
  physical interaction where needed, runs tcpdump + active probes,
  anonymizes the resulting pcap, and writes companion text files).
- `probe.py` — standalone active prober. Useful for ad-hoc tests
  outside the full capture pack:

  ```sh
  python3 research/probe.py discovery       <printer-ip> --out /tmp/probe
  python3 research/probe.py simple          <printer-ip> --out /tmp/probe
  python3 research/probe.py etb-sweep       <printer-ip> --out /tmp/probe --blocks 32
  python3 research/probe.py watch-asb       <printer-ip> --out /tmp/probe --hold 90
  python3 research/probe.py hold-9101       <printer-ip> --port 9101 --out /tmp/probe --hold 60
  python3 research/probe.py replay-shopify  <printer-ip> --out /tmp/probe
  ```

- `anonymize.py` — pure-stdlib pcap rewriter. Substitutes printer/host
  MACs and IPs to RFC 5737 / locally-administered synthetic values and
  recomputes IPv4/UDP/TCP checksums. Has `--text-only` mode for
  rewriting companion text files.

## Reproducing a capture from scratch

```sh
# from the repo root, with a real Star LAN printer reachable on the LAN
./research/capture.sh <iface> <printer-ip> --scenario <name>

# sanity-check the script with no printer present
./research/capture.sh <iface> 192.0.2.123 --dry-run
```

Required: `tcpdump`, `python3`. Optional but recommended: `tshark`,
`nmap`, `wget`. `tcpdump` needs root — the script will use `sudo`
automatically when run as a regular user, prompting for a password
once at the start of the session.

For each scenario the script:

1. Prompts you (e.g. "OPEN THE COVER mid-job") and waits for Enter.
2. Starts `tcpdump` with a scenario-specific BPF filter to
   `research/raw-unscrubbed/<scenario>/capture.pcap`.
3. Runs the relevant active probe — `probe.py` for 9100/9101 sequences,
   plus `nmap` / `wget` etc. as appropriate.
4. Stops the capture, runs `anonymize.py` to write the scrubbed
   `research/<scenario>/capture.pcap`, generates `capture.pcap.txt`
   (`tcpdump -nXXr`) and `capture.decoded.txt` (`tshark -V` if installed).
5. Re-applies the same substitutions to all companion text files
   (`probe.log`, `frames.log`, etc.) so hex dumps stay consistent.
6. Writes `meta.txt` with timestamp, host OS, and the substitution map.

## Anonymization workflow

`anonymize.py` rewrites a classic libpcap (Ethernet/IPv4) so the printer
MAC, host MAC, printer IP, host IP, and gateway IP are replaced with fixed
synthetic values from the documentation ranges (RFC 5737 IPs,
locally-administered MACs). IPv4, UDP, and TCP checksums are recomputed.

Synthetic substitutions used across the pack so cross-pcap correlation
survives:

| Original              | Synthetic            |
|-----------------------|----------------------|
| Printer MAC           | `02:00:00:00:00:01`  |
| Host (capture) MAC    | `02:00:00:00:00:02`  |
| Printer IP            | `192.0.2.10`         |
| Host (capture) IP     | `192.0.2.20`         |
| Default gateway IP    | `192.0.2.1`          |

The `shopify-replay/` scenario uses additional synthetic addresses
(`.30` and `.40`) because that capture spans two L2 segments
(phone↔NAT-gateway and NAT-gateway↔printer) — see its `meta.txt`.

`pcapng` is **not** supported. Convert first if needed:
`tcpdump -r in.pcapng -w in.pcap`. Link layer must be Ethernet — capture
from a wired host upstream of the AP rather than a Wi-Fi monitor-mode
adapter.

Hostnames embedded in mDNS / DHCP payloads are **not** rewritten in the
binary pcap (the DNS length-prefix fields would have to be recomputed).
After running, sanity-check with:

```sh
strings research/<scenario>/capture.pcap | grep -i "<printer-serial>"
```

Redact by hand (or just delete the affected scenario) if anything
identifying remains.

`research/raw-unscrubbed/` is gitignored. Delete it before committing,
or treat it as scratch space for the session.

## Verification checklist

Before committing newly-captured scenarios:

- [ ] Each new scenario subdir contains `capture.pcap`, `capture.pcap.txt`,
      `notes.md`, and `meta.txt`.
- [ ] `grep -rE '([0-9]{1,3}\.){3}[0-9]{1,3}' research/` returns only
      `192.0.2.x` addresses (no original IPs leaked, except where
      `meta.txt` explicitly documents the synthetic substitution map).
- [ ] `grep -riE '([0-9a-f]{2}:){5}[0-9a-f]{2}' research/` returns only
      synthetic `02:00:00:00:00:0[1-4]` MACs (same exception).
- [ ] `research/raw-unscrubbed/` is empty (or deleted) before commit.
- [ ] `meta.txt` records firmware version and host OS.
- [ ] `./capture.sh <iface> 192.0.2.123 --dry-run` exits cleanly.

The real success criterion: a future maintainer **without a printer** can
read the cross-cutting `notes-*.md` files and reconstruct the byte-level
behaviour of any captured surface. If they can, the pack works.
