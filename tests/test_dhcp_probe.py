import ipaddress
import socket
import threading
import unittest

from simplenetinstallppc.net.dhcp_packet import (BOOTREPLY, FLAG_BROADCAST, DhcpPacket, MessageType,
                                            Opt)
from simplenetinstallppc.net.dhcp_probe import (DhcpAnswer, ProbeUnavailable, assess,
                                           build_discover, parse_answer, probe)

from capture import FIXTURES, read_udp

IP = ipaddress.IPv4Address
MAC = "84:ba:59:00:00:d4"
POOL = (IP("192.168.50.50"), IP("192.168.50.59"))


def offer(xid, server="192.168.50.253", yiaddr="192.168.50.114"):
    packet = DhcpPacket(op=BOOTREPLY, xid=xid, yiaddr=IP(yiaddr))
    packet.options[Opt.MESSAGE_TYPE] = bytes((MessageType.OFFER,))
    packet.options[Opt.SERVER_ID] = IP(server).packed
    packet.options[Opt.ROUTER] = IP(server).packed
    packet.options[Opt.DNS] = IP(server).packed + IP("8.8.8.8").packed
    return packet.encode()


class ParseTests(unittest.TestCase):
    def test_discover(self):
        packet = DhcpPacket.decode(build_discover(bytes.fromhex(MAC.replace(":", "")), 7).encode())
        self.assertEqual(packet.message_type, MessageType.DISCOVER)
        self.assertEqual(packet.mac, MAC)
        self.assertEqual(packet.xid, 7)
        self.assertTrue(packet.flags & FLAG_BROADCAST)

    def test_offer_captured_from_our_server(self):
        captured = [p for p in read_udp(FIXTURES / "imac-g3-boots.pcap")
                    if p.sport == 67 and DhcpPacket.decode(p.payload).message_type
                    == MessageType.OFFER]
        first = captured[0]
        xid = DhcpPacket.decode(first.payload).xid
        answer = parse_answer(first.payload, xid, first.src)
        self.assertEqual(answer, DhcpAnswer(server=IP("192.168.50.104"), source="192.168.50.104",
                                            offered=IP("192.168.50.50"),
                                            router=IP("192.168.50.253"),
                                            dns=[IP("192.168.50.253")]))
        self.assertIsNone(parse_answer(first.payload, xid + 1, first.src))

    def test_only_offers_count(self):
        packet = DhcpPacket.decode(offer(5))
        packet.options[Opt.MESSAGE_TYPE] = bytes((MessageType.ACK,))
        self.assertIsNone(parse_answer(packet.encode(), 5, "192.168.50.253"))
        self.assertIsNone(parse_answer(b"junk", 5, "192.168.50.253"))

    def test_describe(self):
        answer = parse_answer(offer(5), 5, "192.168.50.253")
        self.assertEqual(answer.describe(), "192.168.50.253 (offered 192.168.50.114, "
                                            "router 192.168.50.253, DNS 192.168.50.253, 8.8.8.8)")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LoopbackProbeTests(unittest.TestCase):
    """A fake DHCP server on 127.0.0.1 that answers every DISCOVER twice, as two servers."""

    def setUp(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.settimeout(0.05)
        self.stopping = threading.Event()
        self.server_port = self.server.getsockname()[1]
        self.client_port = free_port()
        self.discovers = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stopping.is_set():
            try:
                data, (host, port) = self.server.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            request = DhcpPacket.decode(data)
            self.discovers += 1
            for server in ("192.168.50.253", "192.168.50.1"):
                self.server.sendto(offer(request.xid, server), (host, port))

    def tearDown(self):
        self.stopping.set()
        self.thread.join(2)
        self.server.close()

    def run_probe(self, **kwargs):
        return probe(None, MAC, client_port=self.client_port, server_port=self.server_port,
                     target="127.0.0.1", timeout=0.4, **kwargs)

    def test_one_answer_per_server(self):
        answers = self.run_probe()
        self.assertEqual([str(a.server) for a in answers], ["192.168.50.253", "192.168.50.1"])
        self.assertEqual(self.discovers, 2)       # the retry halfway through

    def test_own_address_is_ignored(self):
        self.assertEqual(self.run_probe(ignore=["127.0.0.1"]), [])

    def test_port_in_use(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
            busy.bind(("0.0.0.0", self.client_port))      # no SO_REUSEADDR
            with self.assertRaises(ProbeUnavailable):
                self.run_probe()


class AssessTests(unittest.TestCase):
    router = DhcpAnswer(server=IP("192.168.50.253"), source="192.168.50.253",
                        offered=IP("192.168.50.114"))

    def test_bsdp_only_needs_a_dhcp_server(self):
        [(level, message)], refusal = assess([], "bsdp-only", "netboot")
        self.assertEqual(level, 30)
        self.assertIn("no DHCP server answered", message)
        self.assertIsNone(refusal)
        [(level, _)], refusal = assess([self.router], "bsdp-only", "netboot")
        self.assertEqual((level, refusal), (20, None))

    def test_full_netboot_next_to_a_router(self):
        [(level, message)], refusal = assess([self.router], "full", "netboot", POOL)
        self.assertEqual(level, 20)
        self.assertIn("answering NetBoot Macs only", message)
        self.assertIsNone(refusal)

    def test_overlapping_pool_refuses(self):
        inside = DhcpAnswer(server=IP("192.168.50.1"), source="192.168.50.1",
                            offered=IP("192.168.50.55"))
        _, refusal = assess([self.router, inside], "full", "netboot", POOL)
        self.assertIn("overlaps", refusal)
        self.assertIn("192.168.50.1 offered 192.168.50.55", refusal)

    def test_answer_all_next_to_another_server_refuses(self):
        _, refusal = assess([self.router], "full", "all", POOL)
        self.assertIn("--dhcp-clients netboot", refusal)
        self.assertEqual(assess([], "full", "all", POOL)[1], None)


if __name__ == "__main__":
    unittest.main()
