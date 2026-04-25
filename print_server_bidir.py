#!/usr/bin/env python3
# Copyright (c) 2026 Senternal LLC <https://senternaltech.com>
# SPDX-License-Identifier: MIT
"""Bidirectional TCP-to-USB bridge for Star Micronics TSP143IIIU.

Bridges a USB-connected Star thermal receipt printer to the network so that
StarIO-based POS applications (e.g. Shopify POS on iPad) can discover and
print to it as if it were a native Star LAN printer (TSP100IIILAN).

Architecture:
    The bridge exposes two TCP ports, mirroring a real Star LAN printer:

    - Port 9100 (print):  Bidirectional data channel. POS app sends Star
                          raster print data; printer sends back ASB (Automatic
                          Status Back) frames reporting its state.

    - Port 9101 (status): Short-lived status polling channel. POS app connects,
                          sends a 51-byte query, receives two 11-byte ASB
                          frames, then disconnects. This is polled every ~220ms
                          during printing to monitor job progress.

    Both ports talk to /dev/usb/lp0 (the USB printer) via non-blocking I/O.
    A single select() event loop multiplexes all TCP and USB file descriptors.

StarIO Protocol Overview:
    Star printers use the ASB (Automatic Status Back) protocol for bidirectional
    communication. Key concepts:

    - ASB Frames: Variable-length status packets (typically 9 bytes from USB,
      11 bytes from LAN printers). Byte 0 encodes the frame length. Byte 1
      contains the protocol version. Byte 2 has the ETB-executed flag. Byte 7
      carries the ETB counter. Remaining bytes report errors, paper status, etc.

    - ETB (0x17): End-of-Transmission-Block marker. The POS app embeds ETB
      bytes in the print data stream. The printer increments an internal counter
      each time it processes an ETB. The POS app watches the counter (via ASB
      frames on both port 9100 and 9101) to confirm data was received.

    - ESC RS E 0 (1B 1E 45 00): Clears the ETB counter to zero. Sent by the
      POS app at the start of each print job to establish a baseline.

    - ESC RS a 1 (1B 1E 61 01): Enables unsolicited ASB on the data channel.
      The printer will send ASB frames automatically when its state changes.

    - ESC ACK SOH (1B 06 01): One-shot status request (used when ASB is off).

USB vs LAN ASB Differences:
    The USB printer (TSP143IIIU) sends 9-byte ASB frames with byte 1 = 0x06.
    The LAN printer (TSP100IIILAN) sends 11-byte frames with byte 1 = 0x86
    (bit 7 set, indicating a network interface). This bridge transforms USB
    frames to LAN format by padding to 11 bytes and setting byte 1 bit 7,
    so the POS app sees responses identical to a real LAN printer.

Critical Design Decision — No Synthetic ASB:
    An earlier version of this bridge tried to generate synthetic ASB responses
    immediately when it saw ETB markers in the data stream, without waiting for
    the real printer. This caused the ETB counter on port 9100 (synthetic) and
    port 9101 (also synthetic) to desync from the real printer's counter. Shopify
    POS polls port 9101 every ~220ms and compares the ETB counter to detect when
    the printer finishes processing. If the counters don't match reality, Shopify
    thinks the job failed and retries — causing duplicate prints.

    The correct approach: forward ALL data to the USB printer unmodified, let it
    process ETBs and update its own counter, then forward the real ASB frames
    back to the TCP client. The printer IS the single source of truth for status.

Port 9101 Protocol Detail:
    Shopify's StarIO SDK uses port 9101 as a rapid-polling status channel:

    1. Connect TCP to port 9101
    2. Send 51 bytes: 0x32 followed by 50 zero bytes
    3. Receive 22 bytes: two consecutive 11-byte ASB frames
    4. Server closes the connection (FIN)

    This cycle repeats every ~220ms during active printing. Shopify uses the
    ETB counter in these frames to track print job progress. The counter must
    reflect the real printer's state — any desync causes Shopify to retry.

    Between print jobs, Shopify probes with 2-3 connect/disconnect cycles
    before committing to send data. This is normal behavior observed on both
    real LAN printers and this bridge.

No CUPS Dependency:
    Shopify POS sends Star-native raster data directly. The printer accepts
    raw StarLine commands over USB without any spooler or driver. Ensure any
    CUPS queue for this printer is disabled or removed, otherwise CUPS will
    hold an exclusive lock on /dev/usb/lp0.

Usage:
    python3 print_server_bidir.py [-p PORT] [-s STATUS_PORT] [-d DEVICE] [-v]

    Typically run as a systemd service. See tsp143-bridge.service and install.sh.
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import select
import signal
import socket
import sys
import threading
import time

from stario_proto import StarStatus, StatusTracker

logger = logging.getLogger("print_server")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PORT = 9100       # TCP port for print data (bidirectional)
STATUS_PORT = 9101        # TCP port for status polling
USB_DEVICE = "/dev/usb/lp0"
BUFFER_SIZE = 4096
USB_WRITE_BUF_MAX = 1024 * 1024  # 1 MiB — drop data beyond this to prevent OOM

# Star command sequences that we intercept (not forwarded to USB)
#   ESC RS a 1 — enable ASB.  We manage USB ASB ourselves at startup.
#   ESC RS a 0 — disable ASB. We never disable it; we need ASB always on.
# All other commands (including ETB, ESC RS E 0, ESC @, etc.) pass through
# to the printer unmodified so it can maintain its own internal state.
_STRIPPED_COMMANDS = [
    (b"\x1b\x1e\x61\x01", "ESC RS a 1 (enable ASB)"),
    (b"\x1b\x1e\x61\x00", "ESC RS a 0 (disable ASB)"),
]


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def open_usb(path: str) -> int:
    """Open the USB printer device for bidirectional non-blocking I/O.

    The device must be opened with O_RDWR for bidirectional communication
    (we need to both send print data and receive ASB status frames) and
    O_NONBLOCK so reads/writes never stall the event loop.
    """
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    logger.info("Opened %s (fd=%d)", path, fd)
    return fd


def init_printer(usb_fd: int):
    """Enable ASB (Automatic Status Back) on the USB printer.

    Sends ESC RS a 1 to enable unsolicited ASB on the USB bulk-IN pipe.
    Once enabled, the printer sends a 9-byte ASB frame whenever its state
    changes (paper out, cover open, job complete, etc.).

    We intentionally do NOT send ESC @ (printer reset) because that would
    abort any in-progress print job if the bridge service is restarted
    while the printer is mid-receipt.
    """
    try:
        os.write(usb_fd, b"\x1b\x1e\x61\x01")
        logger.info("ASB enabled on USB printer")
    except OSError as e:
        logger.warning("ASB enable failed: %s (printer may be busy)", e)


def make_server(port: int) -> socket.socket:
    """Create a non-blocking TCP server socket bound to all interfaces."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setblocking(False)
    sock.bind(("0.0.0.0", port))
    sock.listen(5)
    return sock


def asb_frame_size(header: int) -> int:
    """Decode ASB frame size from the header byte (byte 0).

    The frame length is encoded in non-contiguous bits of the header:
        - Bits 1-3: low 3 bits of the size
        - Bits 5-6: high 2 bits of the size
        - Bit 0:    always 1 (frame marker — distinguishes ASB from data)
        - Bit 4:    always 0
        - Bit 7:    unused

    Examples:
        0x23 (00100011) → size = 9  (standard USB frame)
        0x25 (00100101) → size = 10
        0x27 (00100111) → size = 11

    The USB TSP143IIIU sends 9-byte frames (header 0x23).
    The LAN TSP100IIILAN sends 11-byte frames (also header 0x23, but the
    actual payload is 11 bytes — the size field doesn't fully account for
    the extra LAN-specific bytes).
    """
    return ((header & 0b1110) >> 1) | ((header & 0b1100000) >> 2)


def extract_asb_frame(data: bytes) -> tuple[bytes | None, bytes]:
    """Extract one ASB status frame from the beginning of a byte buffer.

    ASB frames are self-delimiting: byte 0 has bit 0 set (distinguishing
    it from print data) and encodes the frame length. This function peels
    off one complete frame and returns it along with any remaining bytes.

    Returns:
        (frame_bytes, remainder) if a complete frame was found
        (None, original_data)   if the data doesn't start with a valid frame

    USB bulk transfers usually deliver complete frames, but data can arrive
    concatenated (e.g., two ASB frames in one read), so callers should loop.
    """
    if not data:
        return None, b""
    size = asb_frame_size(data[0])
    if size >= 2 and len(data) >= size:
        return bytes(data[:size]), bytes(data[size:])
    return None, bytes(data)


def usb_asb_to_lan_format(usb_asb: bytes) -> bytes:
    """Transform a USB ASB frame (9 bytes) to LAN format (11 bytes).

    The LAN TSP100IIILAN sends 11-byte ASB frames with byte 1 bit 7 set
    (0x86 instead of 0x06). StarIO clients (including Shopify POS) expect
    this format from network printers. If we send 9-byte USB-format frames,
    the client may misparse them or reject the printer.

    Transformation:
        1. Set bit 7 of byte 1 (version byte) to indicate LAN interface
        2. Pad to 11 bytes with zeros if the USB frame was shorter
        3. Truncate to exactly 11 bytes
    """
    asb = bytearray(usb_asb)
    asb[1] |= 0x80           # Set LAN interface flag (bit 7 of version byte)
    while len(asb) < 11:     # Pad to 11 bytes
        asb.append(0x00)
    return bytes(asb[:11])    # Ensure exactly 11 bytes


# ---------------------------------------------------------------------------
# Main bridge class
# ---------------------------------------------------------------------------

class BidirectionalBridge:
    """Multiplexes TCP 9100 (print), TCP 9101 (status), and USB via select().

    Data flow:

        Shopify POS (iPad)                    Bridge                   USB Printer
        ──────────────────                    ──────                   ───────────
        TCP 9100 ──print data──────────────→ forward ──────────────→ /dev/usb/lp0
                 ←──ASB frames (LAN format)── transform ←──ASB (USB format)──
        TCP 9101 ──51-byte query──────────→ respond with cached ASB
                 ←──2x 11-byte ASB frames── (from last USB ASB, converted to LAN)

    All Star commands (ETB, ESC RS E 0, ESC @, etc.) pass through to the
    printer unmodified. The printer maintains its own ETB counter and sends
    ASB frames reporting the real counter value. We cache the latest ASB
    and serve it on both port 9100 (forwarded) and port 9101 (on demand).
    """

    def __init__(self, usb_path: str, print_port: int, status_port: int):
        self.usb_path = usb_path
        self.print_port = print_port
        self.status_port = status_port

        self._running = False
        self._usb_fd: int = -1

        # The single active TCP client on port 9100. Only one client can
        # print at a time — new connections replace the previous one.
        self._print_client: socket.socket | None = None

        # Most recent ASB frame received from the USB printer. Used as the
        # basis for 9101 status responses and forwarded to the 9100 client.
        # Contains real printer state: paper, cover, errors, ETB counter.
        self._last_asb: bytes = b""

        # Non-blocking write buffer for the TCP print client. Data is
        # queued here when send() would block, and drained by select().
        self._print_write_buf: bytearray = bytearray()

        # Non-blocking write buffer for the USB device. Queued when the
        # USB fd would block (printer processing), drained by select().
        self._usb_write_buf: bytearray = bytearray()

        # Status trackers for structured logging — decode ASB frames and
        # report which fields changed since the last frame.
        self._real_tracker = StatusTracker("real")    # USB → bridge
        self._fwd_tracker = StatusTracker("forward")  # bridge → TCP client

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    def start(self):
        """Start the bridge: open USB, create TCP servers, run event loop."""
        self._running = True
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        # Open USB device — fails if CUPS has it locked
        try:
            self._usb_fd = open_usb(self.usb_path)
        except OSError as e:
            logger.error("Cannot open %s: %s", self.usb_path, e)
            logger.error("Ensure CUPS queue is disabled: sudo cupsdisable TSP143")
            sys.exit(1)

        # Enable ASB so we receive status frames from the printer
        init_printer(self._usb_fd)

        # Read and discard any stale data; cache the initial ASB frame
        self._drain_usb()

        # If no ASB was received during drain, explicitly request one
        # using ESC ACK SOH (one-shot status request)
        if not self._last_asb:
            try:
                os.write(self._usb_fd, b"\x1b\x06\x01")
                time.sleep(0.05)
                self._drain_usb()
            except OSError:
                pass

        # Create TCP server sockets
        print_srv = make_server(self.print_port)
        status_srv = make_server(self.status_port)

        logger.info(
            "Bridge ready: print=%d status=%d usb=%s cached_asb=%s",
            self.print_port, self.status_port, self.usb_path,
            self._last_asb.hex() if self._last_asb else "none",
        )

        try:
            self._event_loop(print_srv, status_srv)
        finally:
            self._cleanup(print_srv, status_srv)

    def _handle_signal(self, signum, frame):
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        logger.info("Received signal %d, shutting down", signum)
        self._running = False

    # -------------------------------------------------------------------
    # USB I/O
    # -------------------------------------------------------------------

    def _drain_usb(self):
        """Read and discard any pending USB data, caching the latest ASB.

        Called at startup to clear stale data from the USB buffer and
        establish an initial ASB baseline. Safety-capped at 20 reads to
        prevent runaway loops if the printer streams continuously.
        """
        for _ in range(20):
            try:
                data = os.read(self._usb_fd, BUFFER_SIZE)
                if not data:
                    break
                asb, _ = extract_asb_frame(data)
                if asb:
                    self._last_asb = asb
                    self._log_real_asb(asb)
            except OSError:
                break

    def _write_to_usb(self, data: bytes):
        """Write data to USB, queuing any overflow for later.

        If the USB device can't accept all bytes immediately (EAGAIN),
        the remainder is buffered in _usb_write_buf and drained by the
        select() loop when the fd becomes writable.

        A maximum buffer size prevents unbounded memory growth if the
        printer stalls (e.g., paper jam, cover open).
        """
        if len(self._usb_write_buf) + len(data) > USB_WRITE_BUF_MAX:
            logger.warning(
                "[usb] Write buffer full (%d bytes), dropping %d bytes "
                "(printer may be stalled)",
                len(self._usb_write_buf), len(data))
            return

        if self._usb_write_buf:
            self._usb_write_buf.extend(data)
            return

        try:
            written = os.write(self._usb_fd, data)
            if written < len(data):
                self._usb_write_buf.extend(data[written:])
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                self._usb_write_buf.extend(data)
            else:
                logger.error("[usb] Write failed: %s", e)

    def _flush_usb_write_buf(self):
        """Drain queued USB writes when the fd is writable."""
        while self._usb_write_buf:
            try:
                written = os.write(self._usb_fd, bytes(self._usb_write_buf))
                del self._usb_write_buf[:written]
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                logger.error("[usb] Buffered write failed: %s", e)
                self._usb_write_buf.clear()
                break

    def _handle_usb_read(self):
        """Read ASB frames from USB and forward to the TCP print client.

        This is the core status forwarding path. When the USB printer sends
        an ASB frame (either in response to a command or unsolicited, e.g.
        after finishing a print job), we:

        1. Cache it as the latest known printer state (_last_asb)
        2. Transform it from USB format (9 bytes, byte1=0x06) to LAN format
           (11 bytes, byte1=0x86) so the POS app sees a native LAN printer
        3. Forward it to the active TCP print client on port 9100

        The cached ASB is also served on port 9101 when the POS app polls.
        This means both ports always reflect the real printer's current state,
        including the ETB counter — which is critical for Shopify to confirm
        job completion and avoid duplicate prints.
        """
        try:
            data = os.read(self._usb_fd, BUFFER_SIZE)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            logger.error("[usb] Read error: %s", e)
            return

        if not data:
            return

        logger.debug("[usb] Read %d bytes: %s", len(data), data.hex())

        # Parse ASB frame(s) from the USB data
        asb, remainder = extract_asb_frame(data)
        if asb:
            self._last_asb = asb
            self._log_real_asb(asb)

            # Forward to the active TCP print client in LAN format
            if self._print_client:
                lan_asb = usb_asb_to_lan_format(asb)
                self._log_fwd_asb(lan_asb)
                self._send_or_queue(
                    self._print_client, self._print_write_buf, lan_asb, "print")

    # -------------------------------------------------------------------
    # TCP: Port 9100 (print data channel)
    # -------------------------------------------------------------------

    def _accept_print(self, srv: socket.socket):
        """Accept a new print client on port 9100.

        Only one print client is active at a time. A new connection replaces
        any existing client. This matches real Star LAN printer behavior.
        """
        try:
            client, addr = srv.accept()
        except OSError:
            return
        client.setblocking(False)

        if self._print_client:
            logger.debug("[print] Replacing existing client")
            self._close_print_client()

        self._print_client = client
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        logger.info("[print] Connection from %s:%d", addr[0], addr[1])

    def _handle_print_data(self):
        """Read data from the TCP print client and forward to USB.

        All data passes through to the USB printer with minimal filtering:
        only ASB enable/disable commands (ESC RS a) are stripped because we
        manage USB ASB ourselves. Everything else — including ETB markers,
        ESC RS E 0 (clear counter), ESC @ (reset), raster data — goes
        straight to the printer so it can maintain its own internal state.

        The printer's ASB responses come back asynchronously via
        _handle_usb_read() and are forwarded to this same TCP client.
        """
        try:
            data = self._print_client.recv(BUFFER_SIZE)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            self._close_print_client()
            return

        if not data:
            logger.info("[print] Client disconnected")
            self._close_print_client()
            return

        # Strip ASB enable/disable commands — we manage USB ASB at startup
        # and keep it always-on. All other Star commands pass through.
        filtered = data
        for seq, label in _STRIPPED_COMMANDS:
            if seq in filtered:
                filtered = filtered.replace(seq, b"")
                logger.info("[print] Stripped %s from client data", label)

        if filtered:
            logger.debug("[print] TCP→USB %d bytes", len(filtered))
            self._write_to_usb(filtered)

    def _close_print_client(self):
        """Close the active print client and clear its write buffer."""
        if self._print_client:
            try:
                self._print_client.close()
            except OSError:
                pass
            self._print_client = None
            self._print_write_buf.clear()

    # -------------------------------------------------------------------
    # TCP: Port 9101 (status polling channel)
    # -------------------------------------------------------------------

    def _accept_status(self, srv: socket.socket):
        """Accept a status client on port 9101 and respond with cached ASB.

        The real TSP100IIILAN handles port 9101 as a rapid-poll status channel:

        1. Client connects
        2. Client sends 51 bytes (0x32 + 50 zero bytes) — status query
        3. Printer responds with two consecutive 11-byte ASB frames (22 bytes)
        4. Printer closes the connection (FIN)

        Shopify POS repeats this cycle every ~220ms during printing to watch
        the ETB counter change, confirming the printer is processing data.

        This handler runs in a daemon thread to avoid blocking the main
        select() loop, since the client may take a moment to send its query.
        """
        try:
            client, addr = srv.accept()
        except OSError:
            return
        logger.info("[status] Connection from %s:%d", addr[0], addr[1])

        def _handle_status(sock, peer_addr):
            try:
                sock.settimeout(2.0)

                # Wait for the client's 51-byte status query
                try:
                    data = sock.recv(1024)
                except (socket.timeout, OSError):
                    data = None

                if data:
                    logger.info("[status] Received %d bytes from %s:%d",
                                len(data), peer_addr[0], peer_addr[1])

                    # Build response: two copies of the latest ASB in LAN format.
                    # If we have a cached USB ASB, transform it. Otherwise send
                    # a clean default frame with no errors.
                    if self._last_asb:
                        asb = usb_asb_to_lan_format(self._last_asb)
                    else:
                        # Default clean ASB: header=0x23, version=0x86 (LAN),
                        # etb_executed=true (0x02), all other fields zero
                        asb = bytes([
                            0x23, 0x86, 0x02, 0x00, 0x00,
                            0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                        ])

                    response = asb + asb  # Two identical ASB frames = 22 bytes
                    sock.sendall(response)
                    logger.info("[status] Sent 2x ASB (%d bytes): %s",
                                len(response), response.hex())

            except OSError as e:
                logger.debug("[status] Error with %s:%d: %s",
                             peer_addr[0], peer_addr[1], e)
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
                logger.info("[status] Closed %s:%d", peer_addr[0], peer_addr[1])

        t = threading.Thread(target=_handle_status, args=(client, addr), daemon=True)
        t.start()

    # -------------------------------------------------------------------
    # TCP: Non-blocking send helpers
    # -------------------------------------------------------------------

    def _send_or_queue(self, client: socket.socket, buf: bytearray,
                       data: bytes, label: str):
        """Try to send data immediately; queue any unsent bytes.

        Non-blocking TCP sends may not accept all bytes at once. Any
        remainder is appended to the write buffer and drained by the
        select() loop when the socket becomes writable.
        """
        if buf:
            buf.extend(data)
            return
        try:
            sent = client.send(data)
            if sent < len(data):
                buf.extend(data[sent:])
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                buf.extend(data)
            else:
                logger.debug("[%s] Send failed: %s", label, e)

    def _flush_client_buf(self, client: socket.socket, buf: bytearray,
                          label: str):
        """Drain queued TCP writes when the socket is writable."""
        while buf:
            try:
                sent = client.send(bytes(buf))
                if sent == 0:
                    break
                del buf[:sent]
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                logger.debug("[%s] Send failed: %s", label, e)
                buf.clear()
                self._close_print_client()
                break

    # -------------------------------------------------------------------
    # Logging helpers
    # -------------------------------------------------------------------

    def _log_real_asb(self, frame: bytes):
        """Log an ASB frame received from the USB printer."""
        st, changes = self._real_tracker.update(frame)
        logger.info("[usb  ASB] %s  %s", frame.hex(), st.summary())
        if changes and changes != ["(initial)"]:
            logger.info("[usb  chg] %s", " | ".join(changes))

    def _log_fwd_asb(self, frame: bytes):
        """Log an ASB frame forwarded to the TCP client."""
        st, changes = self._fwd_tracker.update(frame)
        logger.info("[fwd  ASB] %s  %s", frame.hex(), st.summary())
        if changes and changes != ["(initial)"]:
            logger.info("[fwd  chg] %s", " | ".join(changes))
        if st.errors():
            logger.warning("[fwd  ERR] %s", ",".join(st.errors()))

    # -------------------------------------------------------------------
    # Event loop
    # -------------------------------------------------------------------

    def _event_loop(self, print_srv: socket.socket, status_srv: socket.socket):
        """Main select() loop multiplexing all I/O sources.

        Monitors:
            - print_srv:    new TCP connections on port 9100
            - status_srv:   new TCP connections on port 9101
            - _usb_fd:      ASB frames from the USB printer (read)
            - _print_client: data from the POS app (read) and ASB responses (write)
            - _usb_fd:      buffered print data (write, when queued)
        """
        while self._running:
            rlist: list = [print_srv, status_srv, self._usb_fd]
            wlist: list = []

            if self._print_client:
                rlist.append(self._print_client)
                if self._print_write_buf:
                    wlist.append(self._print_client)

            if self._usb_write_buf:
                wlist.append(self._usb_fd)

            try:
                readable, writable, _ = select.select(rlist, wlist, [], 0.5)
            except (ValueError, OSError):
                self._close_print_client()
                continue

            # Flush buffered writes first
            if self._usb_fd in writable:
                self._flush_usb_write_buf()
            if self._print_client and self._print_client in writable:
                self._flush_client_buf(
                    self._print_client, self._print_write_buf, "print")

            # Process readable fds
            print_client = self._print_client  # snapshot — handler may close
            for fd in readable:
                if fd is print_srv:
                    self._accept_print(print_srv)
                elif fd is status_srv:
                    self._accept_status(status_srv)
                elif fd == self._usb_fd:
                    self._handle_usb_read()
                elif fd is print_client and self._print_client is not None:
                    self._handle_print_data()

    # -------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------

    def _cleanup(self, print_srv: socket.socket, status_srv: socket.socket):
        """Close all sockets and the USB device on shutdown."""
        self._close_print_client()
        for sock in (print_srv, status_srv):
            try:
                sock.close()
            except OSError:
                pass
        if self._usb_fd >= 0:
            try:
                os.close(self._usb_fd)
            except OSError:
                pass
        logger.info("Bridge stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Bidirectional TCP-to-USB bridge for Star TSP143IIIU"
    )
    parser.add_argument(
        "-p", "--port", type=int, default=DEFAULT_PORT,
        help="Print TCP port (default: %(default)s)",
    )
    parser.add_argument(
        "-s", "--status-port", type=int, default=STATUS_PORT,
        help="Status TCP port (default: %(default)s)",
    )
    parser.add_argument(
        "-d", "--device", default=USB_DEVICE,
        help="USB device path (default: %(default)s)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    bridge = BidirectionalBridge(args.device, args.port, args.status_port)
    bridge.start()


if __name__ == "__main__":
    main()
