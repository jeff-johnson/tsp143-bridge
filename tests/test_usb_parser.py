"""Tests for how the bridge splits the USB byte stream.

Run from the repo root:  python3 -m unittest discover tests
"""
import os
import socket
import sys
import time
import unittest
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import print_server_bidir as p  # noqa: E402

logging.disable(logging.CRITICAL)

# Clean USB ASB frame (9 bytes, no errors) as returned by ESC ACK SOH
ASB = bytes.fromhex("230600000000000000")
# Exact reply to ESC # * LF NUL captured from a TSP143IIIU over USB
FIRMWARE_REPLY = bytes.fromhex("1b232a2c54535031303049494955566572312e390a00")


class UsbParserTest(unittest.TestCase):
    def setUp(self):
        self.bridge = p.BidirectionalBridge("/dev/null", 0, 0)
        self.server_end, self.client = socket.socketpair()
        self.server_end.setblocking(False)
        self.client.settimeout(0.2)
        self.bridge._print_client = self.server_end

    def tearDown(self):
        self.server_end.close()
        self.client.close()

    def received(self):
        try:
            return self.client.recv(4096)
        except socket.timeout:
            return b""

    def test_header_check(self):
        self.assertFalse(p.is_asb_header(0x1B))   # ESC decodes to size 5
        self.assertTrue(p.is_asb_header(0x23))
        self.assertEqual(p.asb_frame_size(0x23), 9)

    def test_firmware_reply_not_cached_as_status(self):
        self.bridge._last_asb = ASB
        self.bridge._process_usb_bytes(FIRMWARE_REPLY)
        self.assertEqual(self.bridge._last_asb, ASB)
        self.assertEqual(self.received(), FIRMWARE_REPLY)

    def test_reply_then_asb_in_one_read(self):
        self.bridge._process_usb_bytes(FIRMWARE_REPLY + ASB)
        self.assertEqual(self.bridge._last_asb, ASB)
        self.assertEqual(self.received(),
                         FIRMWARE_REPLY + p.usb_asb_to_lan_format(ASB))

    def test_reply_split_across_reads(self):
        self.bridge._process_usb_bytes(FIRMWARE_REPLY[:5])
        self.assertEqual(self.received(), b"")
        self.bridge._process_usb_bytes(FIRMWARE_REPLY[5:] + ASB)
        self.assertEqual(self.received(),
                         FIRMWARE_REPLY + p.usb_asb_to_lan_format(ASB))

    def test_asb_split_across_reads(self):
        self.bridge._process_usb_bytes(ASB[:4])
        self.assertEqual(self.bridge._last_asb, b"")
        self.bridge._process_usb_bytes(ASB[4:])
        self.assertEqual(self.bridge._last_asb, ASB)

    def test_unterminated_reply_flushed_after_timeout(self):
        self.bridge._process_usb_bytes(b"\x1b#*,TSP")
        time.sleep(p.CMD_REPLY_TIMEOUT + 0.05)
        self.bridge._check_usb_rx_timeout()
        self.assertEqual(self.received(), b"\x1b#*,TSP")
        self.assertFalse(self.bridge._usb_rx)

    def test_lan_status_format(self):
        self.assertEqual(p.usb_asb_to_lan_format(ASB).hex(),
                         "2386000000000000000000")


if __name__ == "__main__":
    unittest.main()
