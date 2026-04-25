# portscan — full TCP + UDP top-50 fingerprint

## TCP ports open

```
23/tcp    open  telnet
80/tcp    open  http        CenteHTTPd/1.1
515/tcp   open  printer
9100/tcp  open  jetdirect?
9101/tcp  open  jetdirect?
22222/tcp open  easyengine?
```

Six open ports; the rest of the 65535 are RST'd cleanly (35 returned
no-response, likely transient retry timeouts).

### Telnet (TCP/23) — undocumented diagnostic surface

The Telnet listener is a **TSP100III Diagnostic Utility** with a banner
on connect:

```
Welcome to TSP100III TELNET Utility.
Copyright(C) 2016 Star Micronics co., Ltd.

<< Connected Device >>
   Device Model : TSP143IIILAN (STR_T-001)
   MAC Address  : 00:11:62:13:DE:EF

login:
```

The banner *itself* leaks the model and MAC pre-auth — useful for
fingerprinting; mildly bad for opsec on a public-facing printer (don't
expose this to the internet). Default credentials were not investigated
in this scan; presumably the same `root:public` as the web admin.

This Telnet utility is **not mentioned** in any Star public docs we
found. Worth knowing for diagnostics and worth flagging as an
unintentional info-disclosure surface.

### LPD (TCP/515)

Standard Berkeley LPR. Provides an alternative path to TCP/9100 for
print jobs from systems that prefer LPR (most non-Star drivers, CUPS
configs, etc.). Not exercised here; just noted as available.

### TCP/22222

nmap's service-version probe couldn't fingerprint this (`easyengine?`
is a guess). Confirmed elsewhere (research/notes-discovery-tsp143iiilan.md)
as the **NIC reset / Star configuration channel** — a TCP twin of the
UDP discovery port. Not part of the discovery flow.

## UDP top 50

```
1900/udp   open|filtered  upnp
2048/udp   open|filtered  dls-monitor
4500/udp   open|filtered  nat-t-ike
5060/udp   open|filtered  sip
32768/udp  open|filtered  omad
49153/udp  open|filtered  unknown
49154/udp  open|filtered  unknown
```

`open|filtered` from nmap means "I sent a probe, got no response, can't
distinguish open from filtered." Combined with our exhaustive UDP
testing in `research/notes-discovery-tsp143iiilan.md` (UDP/22222 is the
ONE working UDP port; SNMP/161, mDNS/5353, Epson/3289 all silent),
**this firmware effectively has no UDP services beyond Star Discovery**.
The "open|filtered" entries above are nmap's default-list of common UDP
ports it tries; their listing here means the printer didn't reply, not
that those services exist.

## Files

- `nmap-tcp-full.log` — full scan output (all 65535 ports tested, with
  service-version detection)
- `nmap-udp-top50.log` — UDP top-50 scan
- `meta.txt`
