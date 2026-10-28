#!/usr/bin/env python3
# Copyright (c) 2026 Senternal LLC <https://senternaltech.com>
# SPDX-License-Identifier: MIT
"""Star Micronics network discovery responder.

StarIO clients (including Shopify POS) discover Star printers on the LAN by
sending UDP broadcasts. This service responds so the Pi-bridged printer
appears automatically in Shopify's printer list.

Star's discovery protocol:
- Client broadcasts a UDP packet to port 22222 containing "STR_BCAST"
- Printer responds with its identity string including model, MAC, and config
"""

import argparse
import fcntl
import logging
import socket
import signal
import struct
import sys
import uuid

logger = logging.getLogger("star_discovery")

DISCOVERY_PORT = 22222
BUFFER_SIZE = 1024
DISCOVERY_MAGIC = b"STR_BCAST"
# Real Star printers send a 28-byte structured query: 16-byte STR_BCAST cell
# + "RQ1.0.0\0" version + 0x001C length + 2-byte request id. The response
# template at 0x10 mirrors this with "RS1.0.1\0". See research/notes-discovery-tsp143iiilan.md.
REQUEST_VERSION_PREFIX = b"RQ1."  # accept RQ1.* request versions
REQUEST_VERSION_OFFSET = 0x10
REQUEST_VERSION_LEN = 4


def get_mac_address() -> str:
    """Get a MAC address string for the response."""
    mac = uuid.getnode()
    return ":".join(f"{(mac >> (8 * i)) & 0xFF:02X}" for i in reversed(range(6)))


def get_local_ip() -> str:
    """Get the local IP address used for LAN communication."""
    try:
        # Connect to a public IP to determine which local interface is used
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "0.0.0.0"


_SIOCGIFADDR = 0x8915
_SIOCGIFNETMASK = 0x891B


def get_network_info(ip: str) -> tuple[bytes, bytes]:
    """Return (netmask_bytes, gateway_bytes) for the interface carrying `ip`.

    Reads the live netmask via SIOCGIFNETMASK ioctl on an AF_INET socket
    (no AF_NETLINK — works under systemd sandboxes that restrict address
    families) and the default gateway via /proc/net/route. Falls back to
    /24 mask and IP-with-last-octet-1 if detection fails.

    The fallback exists because this responder ran for a long time on a
    /24 home network and the legacy template hardcoded those values; new
    deployments on /22 mesh networks (and isolated /24 segments without
    a real default gateway) need the live values to keep Shopify happy.
    """
    mask_bytes = b"\xff\xff\xff\x00"
    gw_bytes = socket.inet_aton(ip.rsplit(".", 1)[0] + ".1")
    try:
        with open("/proc/net/dev") as f:
            next(f); next(f)
            ifnames = [line.split(":", 1)[0].strip() for line in f if ":" in line]
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for ifname in ifnames:
                if ifname == "lo":
                    continue
                ifreq = struct.pack("256s", ifname.encode()[:15])
                try:
                    addr_resp = fcntl.ioctl(probe.fileno(), _SIOCGIFADDR, ifreq)
                except OSError:
                    continue
                if socket.inet_ntoa(addr_resp[20:24]) == ip:
                    mask_resp = fcntl.ioctl(probe.fileno(), _SIOCGIFNETMASK, ifreq)
                    mask_bytes = bytes(mask_resp[20:24])
                    break
        finally:
            probe.close()
    except Exception:
        pass
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    gw_int = int.from_bytes(bytes.fromhex(parts[2]), "little")
                    gw_bytes = gw_int.to_bytes(4, "big")
                    break
    except Exception:
        pass
    return mask_bytes, gw_bytes


def is_structured_query(data: bytes) -> bool:
    """True if `data` matches the well-formed 28-byte StarPRNT discovery query.

    Real Star printers (verified against TSP143IIILAN firmware V2.2)
    require this exact structure or they ignore the packet:

        0x00..0x0F  "STR_BCAST" + 7 NUL    (magic + 16-byte cell)
        0x10..0x17  "RQ1.0.0\\0"            (request version, mirrors response "RS1.0.1")
        0x18..0x19  0x00 0x1C               (length = 28, big-endian)
        0x1A..0x1B  request id              (arbitrary 16-bit; not echoed back)

    See research/notes-discovery-tsp143iiilan.md for the full reverse-
    engineering story.
    """
    if len(data) < 28:
        return False
    if data[:9] != DISCOVERY_MAGIC:
        return False
    if data[9:0x10] != b"\x00" * 7:
        return False
    if data[REQUEST_VERSION_OFFSET:REQUEST_VERSION_OFFSET + REQUEST_VERSION_LEN] != REQUEST_VERSION_PREFIX:
        return False
    if data[0x18:0x1A] != b"\x00\x1C":
        return False
    return True


def is_discovery_query(data: bytes) -> bool:
    """Accept either the well-formed 28-byte form or any packet containing
    the STR_BCAST magic. The loose path stays as a fallback for older clients
    and for our own diagnostic tooling that may send shorter queries; real
    Star LAN printers ignore the loose form, but the bridge accepts both
    so it can stand in for either modern Shopify or ad-hoc tests."""
    return is_structured_query(data) or DISCOVERY_MAGIC in data


def build_response(ip: str, mac: str, model: str, port: int) -> bytes:
    """Build a Star discovery response packet.

    302-byte format captured byte-for-byte from a real TSP100IIILAN printer
    (firmware V2.2). The template is used verbatim with only dynamic fields
    (IP, MAC, model name, gateway) patched in. This ensures Shopify POS
    sees an identical discovery response to a real Star LAN printer.

    Dynamic fields patched into the template:
      0x24 (16): Short model name, null-padded
      0x4E  (6): MAC address
      0x58  (4): IP address
      0x6C  (4): Subnet mask (read from live interface; falls back to /24)
      0x70  (4): Default gateway (read from /proc/net/route; falls back to .1)
      0xCC (64): Full model name + " (STR_T-001)", null-padded
    """
    # Template captured from a real TSP100IIILAN (firmware V2.2).
    # Every byte matches the real printer's response except the fields
    # listed above which are device-specific.
    resp = bytearray(bytes.fromhex(
        "5354525f424341535400000000000000"  # 0x00: "STR_BCAST" header
        "5253312e302e3100012e00220074008a"  # 0x10: "RS1.0.1" version + offset table
        "012c00525453503130304c414e000000"  # 0x20: offset table + short model name
        "0000000056322e320000000056322e32"  # 0x30: firmware versions "V2.2"
        "00000000200000000000000031000011"  # 0x40: flags + MAC start
        "6213deef00000000c0a8008444484350"  # 0x50: MAC + IP + "DHCP"
        "000000000000000000000000ffffff00"  # 0x60: padding + subnet mask
        "c0a80001001631000000000000000000"  # 0x70: gateway + port/flags
        "0000000030003100310000a253746172"  # 0x80: flags + "Star" manufacturer
        "00000000000000000000000000000000"  # 0x90: manufacturer padding
        "00000000000000000000000053544152"  # 0xA0: padding + "STAR" command set
        "00000000000000000000000000000000"  # 0xB0: command set padding
        "00000000000000000000000054535031"  # 0xC0: padding + full model name
        "34334949494c414e20285354525f542d"  # 0xD0: "43IIILAN (STR_T-"
        "30303129000000000000000000000000"  # 0xE0: "001)" + padding
        "00000000000000000000000000000000"  # 0xF0: model name padding
        "0000000000000000000000005052494e"  # 0x100: padding + "PRIN"
        "54455200000000000000000000000000"  # 0x110: "TER" + padding
        "0000000000000000000000000002"      # 0x120: class padding + trailer
    ))

    ip_bytes = socket.inet_aton(ip)
    mac_bytes = bytes.fromhex(mac.replace(":", ""))
    mask_bytes, gw_bytes = get_network_info(ip)

    # Patch dynamic fields into the template

    # 0x24: Short model name (16 bytes, null-padded)
    resp[0x24:0x34] = bytearray(16)
    short = model.encode("ascii")[:16]
    resp[0x24:0x24 + len(short)] = short

    # 0x4E: MAC address (6 bytes)
    resp[0x4E:0x54] = mac_bytes

    # 0x58: IP address (4 bytes, big-endian)
    resp[0x58:0x5C] = ip_bytes

    # 0x6C: Subnet mask (live)
    resp[0x6C:0x70] = mask_bytes

    # 0x70: Default gateway (live)
    resp[0x70:0x74] = gw_bytes

    # 0xCC: Full model identifier (64 bytes, null-padded)
    # Format matches real printer: "TSP143IIILAN (STR_T-001)"
    resp[0xCC:0x10C] = bytearray(64)
    full = f"{model} (STR_T-001)".encode("ascii")[:64]
    resp[0xCC:0xCC + len(full)] = full

    return bytes(resp)


def run(model: str, print_port: int):
    running = True

    def handle_signal(signum, frame):
        nonlocal running
        logger.info("Received signal %d, shutting down", signum)
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    mac = get_mac_address()
    ip = get_local_ip()
    mask_bytes, gw_bytes = get_network_info(ip)
    response = build_response(ip, mac, model, print_port)
    logger.info(
        "Advertising IP: %s, MAC: %s, mask: %s, gw: %s",
        ip, mac,
        socket.inet_ntoa(mask_bytes), socket.inet_ntoa(gw_bytes),
    )

    # Listen on 0.0.0.0 to receive broadcasts, and send responses through
    # the same socket so they come FROM UDP port 22222, as a real Star
    # printer's do. StarIO on Android (e.g. Square Register) silently
    # discards discovery replies whose source port isn't 22222; a separate
    # send socket bound to an ephemeral port made the bridge invisible there.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(1.0)
    sock.bind(("0.0.0.0", DISCOVERY_PORT))

    logger.info(
        "Discovery responder listening on UDP port %d, advertising as %s",
        DISCOVERY_PORT,
        model,
    )

    while running:
        try:
            data, addr = sock.recvfrom(BUFFER_SIZE)
        except socket.timeout:
            continue
        except OSError:
            break

        if not is_discovery_query(data):
            continue
        kind = "structured" if is_structured_query(data) else "loose"
        logger.info("Discovery request (%s) from %s:%d", kind, addr[0], addr[1])
        try:
            sock.sendto(response, addr)
            logger.debug("Sent response from %s:%d to %s:%d",
                         ip, DISCOVERY_PORT, addr[0], addr[1])
        except OSError as e:
            logger.error("Failed to send discovery response: %s", e)

    sock.close()
    logger.info("Discovery responder stopped")


def main():
    parser = argparse.ArgumentParser(description="Star Micronics discovery responder")
    parser.add_argument(
        "-m", "--model", default="TSP143III", help="Model name to advertise (default: TSP143III)"
    )
    parser.add_argument(
        "-p", "--print-port", type=int, default=9100, help="Print server TCP port (default: 9100)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    run(args.model, args.print_port)


if __name__ == "__main__":
    main()
