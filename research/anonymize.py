#!/usr/bin/env python3
# Copyright (c) 2026 Senternal LLC <https://senternaltech.com>
# SPDX-License-Identifier: MIT
"""Anonymize a research-pack pcap (Ethernet/IPv4 only).

Substitutes printer/host MACs, IPs, and gateway IP with fixed synthetic
values from the documentation ranges (RFC 5737 IPs, locally-administered
MACs). Recomputes IPv4, UDP, and TCP checksums after rewriting addresses.
Optionally applies the same string substitutions to companion text files
(e.g. ``capture.pcap.txt``, ``probe.log``) so hex dumps stay consistent.

Pure stdlib. Classic pcap (libpcap) format only — not pcapng. Link layer
must be Ethernet (LINKTYPE 1). For 802.11 monitor-mode captures, capture
from a wired host upstream of the AP instead.

Usage::

    anonymize.py in.pcap out.pcap \\
        --printer-mac AA:BB:CC:DD:EE:FF --printer-ip 192.168.68.85 \\
        --host-mac    11:22:33:44:55:66 --host-ip    192.168.68.50 \\
        --gateway-ip  192.168.68.1 \\
        --also-rewrite capture.pcap.txt --also-rewrite probe.log

Hostnames embedded in mDNS / DHCP payloads are NOT rewritten in the binary
pcap (DNS length-prefix fields would have to be recomputed). Verify with
``strings out.pcap | grep <printer-serial>`` after running, and redact by
hand if anything sensitive remains.
"""

from __future__ import annotations

import argparse
import os
import socket
import struct
import sys


PCAP_MAGIC_LE = b"\xd4\xc3\xb2\xa1"
PCAP_MAGIC_BE = b"\xa1\xb2\xc3\xd4"
LINKTYPE_ETHERNET = 1

ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_ARP = 0x0806
ETHERTYPE_VLAN = 0x8100

PROTO_TCP = 6
PROTO_UDP = 17


def parse_mac(s: str) -> bytes:
    return bytes(int(b, 16) for b in s.split(":"))


def ones_complement_sum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = 0
    for word in struct.unpack(f"!{len(data) // 2}H", data):
        s += word
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def transport_checksum(src_ip: bytes, dst_ip: bytes, proto: int, segment: bytes) -> int:
    pseudo = src_ip + dst_ip + b"\x00" + bytes([proto]) + struct.pack("!H", len(segment))
    return ones_complement_sum(pseudo + segment)


class Mapping:
    def __init__(self, mac_map: dict, ip_map: dict):
        self.mac_map = mac_map
        self.ip_map = ip_map

    def map_mac(self, m: bytes) -> bytes:
        return self.mac_map.get(m, m)

    def map_ip(self, ip: bytes) -> bytes:
        return self.ip_map.get(ip, ip)


def rewrite_arp(payload: bytes, m: Mapping) -> bytes:
    if len(payload) < 28:
        return payload
    out = bytearray(payload)
    out[8:14] = m.map_mac(bytes(out[8:14]))
    out[14:18] = m.map_ip(bytes(out[14:18]))
    out[18:24] = m.map_mac(bytes(out[18:24]))
    out[24:28] = m.map_ip(bytes(out[24:28]))
    return bytes(out)


def rewrite_ipv4(payload: bytes, m: Mapping) -> bytes:
    if len(payload) < 20:
        return payload
    ihl = (payload[0] & 0x0F) * 4
    total_len = struct.unpack("!H", payload[2:4])[0]
    if ihl < 20 or len(payload) < ihl or total_len < ihl:
        return payload

    proto = payload[9]
    new_src = m.map_ip(payload[12:16])
    new_dst = m.map_ip(payload[16:20])

    out = bytearray(payload)
    out[12:16] = new_src
    out[16:20] = new_dst

    # IP header checksum
    out[10:12] = b"\x00\x00"
    out[10:12] = struct.pack("!H", ones_complement_sum(bytes(out[:ihl])))

    # Bound the transport segment by total_len so trailing padding doesn't
    # poison the checksum on short Ethernet frames.
    seg_end = min(total_len, len(out))
    segment = bytearray(out[ihl:seg_end])

    if proto == PROTO_UDP and len(segment) >= 8:
        segment[6:8] = b"\x00\x00"
        csum = transport_checksum(new_src, new_dst, proto, bytes(segment))
        if csum == 0:
            csum = 0xFFFF
        segment[6:8] = struct.pack("!H", csum)
        out[ihl:seg_end] = segment
    elif proto == PROTO_TCP and len(segment) >= 20:
        segment[16:18] = b"\x00\x00"
        csum = transport_checksum(new_src, new_dst, proto, bytes(segment))
        segment[16:18] = struct.pack("!H", csum)
        out[ihl:seg_end] = segment

    return bytes(out)


def rewrite_packet(pkt: bytes, m: Mapping) -> bytes:
    if len(pkt) < 14:
        return pkt
    out = bytearray(pkt)
    out[0:6] = m.map_mac(bytes(out[0:6]))
    out[6:12] = m.map_mac(bytes(out[6:12]))

    etype = struct.unpack("!H", out[12:14])[0]
    offset = 14
    if etype == ETHERTYPE_VLAN:
        if len(out) < 18:
            return bytes(out)
        etype = struct.unpack("!H", out[16:18])[0]
        offset = 18

    payload = bytes(out[offset:])
    if etype == ETHERTYPE_IPV4:
        out[offset:] = rewrite_ipv4(payload, m)
    elif etype == ETHERTYPE_ARP:
        out[offset:] = rewrite_arp(payload, m)
    return bytes(out)


def read_pcap(path: str):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 24:
        raise ValueError("pcap too short")
    magic = data[:4]
    if magic == PCAP_MAGIC_LE:
        endian = "<"
    elif magic == PCAP_MAGIC_BE:
        endian = ">"
    else:
        raise ValueError(
            f"not a classic pcap (magic={magic.hex()}). pcapng is not supported; "
            "convert with `tcpdump -r in.pcapng -w out.pcap` first."
        )
    network = struct.unpack(f"{endian}I", data[20:24])[0]
    if network != LINKTYPE_ETHERNET:
        raise ValueError(f"unsupported link layer {network} (only Ethernet=1)")

    global_hdr = data[:24]
    packets = []
    i = 24
    while i + 16 <= len(data):
        ts_sec, ts_usec, incl_len, orig_len = struct.unpack(f"{endian}IIII", data[i:i + 16])
        i += 16
        if i + incl_len > len(data):
            break
        packets.append((ts_sec, ts_usec, orig_len, data[i:i + incl_len]))
        i += incl_len
    return endian, global_hdr, packets


def write_pcap(path: str, endian: str, global_hdr: bytes, packets) -> None:
    with open(path, "wb") as f:
        f.write(global_hdr)
        for ts_sec, ts_usec, orig_len, pkt in packets:
            f.write(struct.pack(f"{endian}IIII", ts_sec, ts_usec, len(pkt), orig_len))
            f.write(pkt)


def build_mapping(args) -> Mapping:
    mac_map: dict = {}
    ip_map: dict = {}
    if args.printer_mac:
        mac_map[parse_mac(args.printer_mac)] = parse_mac(args.synth_printer_mac)
    if args.host_mac:
        mac_map[parse_mac(args.host_mac)] = parse_mac(args.synth_host_mac)
    if args.printer_ip:
        ip_map[socket.inet_aton(args.printer_ip)] = socket.inet_aton(args.synth_printer_ip)
    if args.host_ip:
        ip_map[socket.inet_aton(args.host_ip)] = socket.inet_aton(args.synth_host_ip)
    if args.gateway_ip:
        ip_map[socket.inet_aton(args.gateway_ip)] = socket.inet_aton(args.synth_gateway_ip)
    return Mapping(mac_map, ip_map)


def rewrite_text(path: str, m: Mapping) -> int:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    original = text
    for orig, synth in m.mac_map.items():
        for sep in (":", "-", ""):
            o_lc = sep.join(f"{b:02x}" for b in orig)
            o_uc = sep.join(f"{b:02X}" for b in orig)
            s_lc = sep.join(f"{b:02x}" for b in synth)
            text = text.replace(o_lc, s_lc).replace(o_uc, s_lc)
    for orig, synth in m.ip_map.items():
        text = text.replace(socket.inet_ntoa(orig), socket.inet_ntoa(synth))
    if text != original:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return len(text) - len(original)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("input_pcap", nargs="?",
                    help="Source pcap (omit when using --text-only)")
    ap.add_argument("output_pcap", nargs="?",
                    help="Destination pcap (omit when using --text-only)")
    ap.add_argument("--text-only", action="store_true",
                    help="Skip pcap rewrite; only apply substitutions to --also-rewrite files")
    ap.add_argument("--printer-mac")
    ap.add_argument("--host-mac")
    ap.add_argument("--printer-ip")
    ap.add_argument("--host-ip")
    ap.add_argument("--gateway-ip")
    ap.add_argument("--synth-printer-mac", default="02:00:00:00:00:01")
    ap.add_argument("--synth-host-mac", default="02:00:00:00:00:02")
    ap.add_argument("--synth-printer-ip", default="192.0.2.10")
    ap.add_argument("--synth-host-ip", default="192.0.2.20")
    ap.add_argument("--synth-gateway-ip", default="192.0.2.1")
    ap.add_argument("--also-rewrite", action="append", default=[],
                    help="Apply same MAC/IP substitutions to this text file (repeatable)")
    args = ap.parse_args()

    mapping = build_mapping(args)
    if not mapping.mac_map and not mapping.ip_map:
        print("no substitutions configured; pass at least --printer-ip", file=sys.stderr)
        sys.exit(2)

    if args.text_only:
        if args.input_pcap or args.output_pcap:
            print("--text-only: do not pass input_pcap/output_pcap", file=sys.stderr)
            sys.exit(2)
        for txt in args.also_rewrite:
            if os.path.exists(txt):
                rewrite_text(txt, mapping)
        print(f"text-only: rewrote {len(args.also_rewrite)} file(s)")
        return

    if not args.input_pcap or not args.output_pcap:
        print("need input_pcap and output_pcap (or use --text-only)", file=sys.stderr)
        sys.exit(2)

    endian, gh, packets = read_pcap(args.input_pcap)
    rewritten = [(ts, us, ol, rewrite_packet(pkt, mapping)) for ts, us, ol, pkt in packets]
    write_pcap(args.output_pcap, endian, gh, rewritten)

    for txt in args.also_rewrite:
        if os.path.exists(txt):
            rewrite_text(txt, mapping)

    print(f"anonymized {len(rewritten)} packets → {args.output_pcap}")
    print(f"  {len(mapping.mac_map)} MAC mapping(s), {len(mapping.ip_map)} IP mapping(s)")


if __name__ == "__main__":
    main()
