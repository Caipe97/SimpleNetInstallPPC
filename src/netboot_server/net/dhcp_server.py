"""DHCP + BSDP server.

Both protocols share UDP port 67, so one server handles both:

* ``bsdp-only`` mode: answers only BSDP INFORM[LIST] / INFORM[SELECT].
  Addresses come from the DHCP server already on the network.
* ``full`` mode: also hands out addresses from a :class:`LeasePool`. It
  serves NetBoot 1.0 firmware clients too, which expect the boot file in the
  plain DHCP ACK.

:meth:`DhcpBsdpServer.handle` does no I/O, so it can be tested without
sockets.
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from ..catalog import Catalog, NetBootImage
from ..events import EventBus
from . import bsdp
from .bsdp import BsdpMessageType, BsdpTag, ClientClass
from .dhcp_packet import BOOTREPLY, BOOTREQUEST, DhcpPacket, MessageType, Opt, ip_list
from .leases import LeasePool

IPv4 = ipaddress.IPv4Address
Destination = Tuple[str, int]
Reply = Tuple[DhcpPacket, Destination]

MAX_VENDOR_OPTION = 255


@dataclass
class DhcpConfig:
    server_ip: IPv4
    subnet: ipaddress.IPv4Network
    mode: str = "bsdp-only"                 # "bsdp-only" or "full"
    pool: Optional[LeasePool] = None
    router: Optional[IPv4] = None
    dns: List[IPv4] = field(default_factory=list)
    server_priority: int = 32768
    nfs_export: Optional[str] = None        # NFS export that holds the images root
    http_base: Optional[str] = None         # e.g. "http://10.0.0.1" (images root)
    broadcast_address: str = "255.255.255.255"
    client_port: int = 68
    server_port: int = 67

    def __post_init__(self) -> None:
        if self.mode not in ("bsdp-only", "full"):
            raise ValueError(f"unknown DHCP mode {self.mode!r}")
        if self.mode == "full" and self.pool is None:
            raise ValueError("full DHCP mode needs an address pool")


@dataclass
class ClientRecord:
    mac: str
    arch: Optional[str] = None
    model: Optional[str] = None
    ip: Optional[str] = None
    last_seen: float = 0.0
    last_message: str = ""
    selected_image: Optional[int] = None


def root_path_for(image: NetBootImage, config: DhcpConfig) -> Optional[str]:
    rel = f"{image.rel_dir}/{image.root_image}"
    if image.protocol == "NFS":
        if not config.nfs_export:
            return None
        return f"nfs:{config.server_ip}:{config.nfs_export}:{rel}"
    if not config.http_base:
        return None
    return f"{config.http_base.rstrip('/')}/{quote(rel)}"


class DhcpBsdpServer(asyncio.DatagramProtocol):
    def __init__(self, config: DhcpConfig, catalog: Catalog, events: EventBus) -> None:
        self.config = config
        self.catalog = catalog
        self.events = events
        self.clients: Dict[str, ClientRecord] = {}
        self.transport: Optional[asyncio.DatagramTransport] = None

    # -- asyncio plumbing ------------------------------------------------------
    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        try:
            packet = DhcpPacket.decode(data)
        except ValueError as exc:
            self.events.emit("dhcp", "malformed", f"ignored malformed packet from {addr[0]}: {exc}",
                             level=10)
            return
        try:
            replies = self.handle(packet)
        except Exception as exc:
            self.events.emit("dhcp", "error", f"error handling packet: {exc!r}",
                             client=packet.mac, level=40)
            return
        assert self.transport is not None
        for reply, destination in replies:
            self.transport.sendto(reply.encode(), destination)

    # -- dispatch ----------------------------------------------------------------
    def handle(self, packet: DhcpPacket) -> List[Reply]:
        if packet.op != BOOTREQUEST:
            return []
        message_type = packet.message_type
        client_class = ClientClass.parse(packet.options.get(Opt.VENDOR_CLASS))
        bsdp_options: Dict[int, bytes] = {}
        if client_class and Opt.VENDOR_SPECIFIC in packet.options:
            try:
                bsdp_options = bsdp.decode_options(packet.options[Opt.VENDOR_SPECIFIC])
            except ValueError as exc:
                self.events.emit("bsdp", "malformed", f"bad BSDP options: {exc}",
                                 client=packet.mac, level=30)
        if client_class:
            self._remember(packet, client_class, message_type)

        if (message_type == MessageType.INFORM and client_class
                and BsdpTag.MESSAGE_TYPE in bsdp_options):
            return self._handle_bsdp(packet, client_class, bsdp_options)

        if message_type is None:
            # Plain BOOTP (no option 53). The Mac OS X 10.0 kernel sends this
            # from nfs_boot to configure its interface before mounting an NFS root.
            if self.config.mode == "full":
                return self._handle_bootp(packet)
            self.events.emit("dhcp", "bootp-ignored",
                             "plain BOOTP request (a Mac OS X 10.0 kernel?) ignored: "
                             "needs --dhcp-mode full, or a DHCP server that answers BOOTP",
                             client=packet.mac, level=30)
            return []

        if self.config.mode != "full":
            if client_class and message_type in (MessageType.DISCOVER, MessageType.REQUEST):
                self.events.emit("dhcp", "ignored",
                                 f"{message_type.name} from Apple client left to the "
                                 "network's DHCP server (bsdp-only mode)",
                                 client=packet.mac, level=10)
            return []
        return self._handle_dhcp(packet, client_class, bsdp_options)

    def _remember(self, packet: DhcpPacket, client_class: ClientClass,
                  message_type: Optional[MessageType]) -> None:
        record = self.clients.setdefault(packet.mac, ClientRecord(packet.mac))
        record.arch = client_class.arch or record.arch
        record.model = client_class.model or record.model
        if int(packet.ciaddr):
            record.ip = str(packet.ciaddr)
        record.last_seen = time.time()
        record.last_message = message_type.name if message_type else "BOOTP"

    # -- BSDP ----------------------------------------------------------------------
    def _handle_bsdp(self, packet: DhcpPacket, client: ClientClass,
                     options: Dict[int, bytes]) -> List[Reply]:
        message = options[BsdpTag.MESSAGE_TYPE][0]
        port = self.config.client_port
        if len(options.get(BsdpTag.REPLY_PORT, b"")) == 2:
            port = int.from_bytes(options[BsdpTag.REPLY_PORT], "big")
        destination = (str(packet.ciaddr) if int(packet.ciaddr)
                       else self.config.broadcast_address, port)
        reply = packet.make_reply(MessageType.ACK, self.config.server_ip)
        reply.ciaddr = packet.ciaddr
        reply.options[Opt.VENDOR_CLASS] = bsdp.VENDOR_CLASS.encode()

        if message == BsdpMessageType.LIST:
            return self._bsdp_list(packet, client, reply, destination)
        if message == BsdpMessageType.SELECT:
            return self._bsdp_select(packet, client, options, reply, destination)
        self.events.emit("bsdp", "unknown", f"unhandled BSDP message type {message}",
                         client=packet.mac, level=30)
        return []

    def _bsdp_list(self, packet: DhcpPacket, client: ClientClass, reply: DhcpPacket,
                   destination: Destination) -> List[Reply]:
        images = self.catalog.available_for(client.arch, client.model)
        if not images:
            self.events.emit("bsdp", "list-empty",
                             f"LIST from {client.model or '?'} ({client.arch or '?'}): "
                             "no enabled image supports this Mac, not answering",
                             client=packet.mac, level=30)
            return []
        default = self.catalog.default_for(client.arch, client.model)
        assert default is not None
        fixed = [
            (BsdpTag.MESSAGE_TYPE, bytes((BsdpMessageType.LIST,))),
            (BsdpTag.SERVER_IDENTIFIER, self.config.server_ip.packed),
            (BsdpTag.SERVER_PRIORITY, bsdp.u16(self.config.server_priority)),
            (BsdpTag.DEFAULT_BOOT_IMAGE, bsdp.u32(default.image_id)),
        ]
        fixed_bytes = bsdp.encode_options(fixed)
        # Keep option 43 within one 255-byte option: unclear whether old
        # firmware reassembles split options (RFC 3396).
        budget = MAX_VENDOR_OPTION - len(fixed_bytes) - 2
        listing, included = bsdp.encode_image_list(
            [(image.image_id, image.name) for image in images], budget)
        if included < len(images):
            self.events.emit("bsdp", "list-truncated",
                             f"image list truncated to {included} of {len(images)} images "
                             "(shorten names or disable images)",
                             client=packet.mac, level=30)
        reply.options[Opt.VENDOR_SPECIFIC] = fixed_bytes + bsdp.encode_options(
            [(BsdpTag.BOOT_IMAGE_LIST, listing)])
        self.events.emit("bsdp", "list",
                         f"LIST from {client.model or '?'} ({client.arch or '?'}): "
                         f"offered {included} image(s), default {default.name!r}",
                         client=packet.mac, images=[i.index for i in images[:included]],
                         default=default.index)
        return [(reply, destination)]

    def _bsdp_select(self, packet: DhcpPacket, client: ClientClass,
                     options: Dict[int, bytes], reply: DhcpPacket,
                     destination: Destination) -> List[Reply]:
        server_id = options.get(BsdpTag.SERVER_IDENTIFIER)
        if server_id and len(server_id) == 4 and IPv4(server_id) != self.config.server_ip:
            self.events.emit("bsdp", "select-other",
                             f"SELECT addressed to another server ({IPv4(server_id)})",
                             client=packet.mac, level=10)
            return []
        selected = options.get(BsdpTag.SELECTED_BOOT_IMAGE, b"")
        image = self.catalog.by_id(int.from_bytes(selected, "big")) if len(selected) == 4 else None
        if image is None or not image.supports(client.arch, client.model):
            return self._bsdp_failed(packet, reply, destination,
                                     f"selected image {selected.hex() or '(none)'} is not available "
                                     f"for {client.model or '?'} ({client.arch or '?'})")
        booter = image.booter_relpath(client.arch)
        root_path = root_path_for(image, self.config)
        if booter is None:
            return self._bsdp_failed(packet, reply, destination,
                                     f"image {image.name!r} has no booter for {client.arch}")
        if root_path is None:
            return self._bsdp_failed(packet, reply, destination,
                                     f"image {image.name!r} uses {image.protocol}, which is not "
                                     "configured on this server")
        if len(booter.encode()) > 127:
            return self._bsdp_failed(packet, reply, destination,
                                     f"booter path too long for the BOOTP file field: {booter}")

        reply.siaddr = self.config.server_ip
        reply.file = booter.encode()
        reply.options[Opt.VENDOR_SPECIFIC] = bsdp.encode_options([
            (BsdpTag.MESSAGE_TYPE, bytes((BsdpMessageType.SELECT,))),
            (BsdpTag.SELECTED_BOOT_IMAGE, bsdp.u32(image.image_id)),
        ])
        reply.options[Opt.ROOT_PATH] = root_path.encode()
        self.clients[packet.mac].selected_image = image.index
        self.events.emit("bsdp", "select",
                         f"SELECT {image.name!r}: booter {booter}, root {root_path}",
                         client=packet.mac, image=image.index, booter=booter,
                         root_path=root_path)
        return [(reply, destination)]

    def _bsdp_failed(self, packet: DhcpPacket, reply: DhcpPacket,
                     destination: Destination, reason: str) -> List[Reply]:
        reply.options[Opt.VENDOR_SPECIFIC] = bsdp.encode_options(
            [(BsdpTag.MESSAGE_TYPE, bytes((BsdpMessageType.FAILED,)))])
        self.events.emit("bsdp", "failed", f"SELECT failed: {reason}", client=packet.mac,
                         level=40)
        return [(reply, destination)]

    # -- full DHCP -------------------------------------------------------------------
    def _handle_dhcp(self, packet: DhcpPacket, client: Optional[ClientClass],
                     bsdp_options: Dict[int, bytes]) -> List[Reply]:
        pool = self.config.pool
        assert pool is not None
        message_type = packet.message_type
        mac = packet.mac

        if message_type == MessageType.DISCOVER:
            ip = pool.offer(mac, packet.option_ip(Opt.REQUESTED_IP))
            if ip is None:
                self.events.emit("dhcp", "exhausted", "address pool exhausted", client=mac,
                                 level=40)
                return []
            reply = packet.make_reply(MessageType.OFFER, self.config.server_ip)
            reply.yiaddr = ip
            self._add_lease_options(reply)
            self._maybe_netboot_1_0(reply, client, bsdp_options, mac)
            self.events.emit("dhcp", "offer", f"OFFER {ip}", client=mac, ip=str(ip))
            return [(reply, self._destination(packet, MessageType.OFFER))]

        if message_type == MessageType.REQUEST:
            server_id = packet.option_ip(Opt.SERVER_ID)
            if server_id is not None and server_id != self.config.server_ip:
                pool.drop_offer(mac)
                return []
            ip = packet.option_ip(Opt.REQUESTED_IP) or packet.ciaddr
            if not int(ip):
                return []
            if pool.confirm(mac, ip):
                reply = packet.make_reply(MessageType.ACK, self.config.server_ip)
                reply.yiaddr = ip
                reply.ciaddr = packet.ciaddr
                self._add_lease_options(reply)
                self._maybe_netboot_1_0(reply, client, bsdp_options, mac)
                if mac in self.clients:
                    self.clients[mac].ip = str(ip)
                self.events.emit("dhcp", "ack", f"ACK {ip}", client=mac, ip=str(ip))
                return [(reply, self._destination(packet, MessageType.ACK))]
            reply = packet.make_reply(MessageType.NAK, self.config.server_ip)
            self.events.emit("dhcp", "nak", f"NAK for requested {ip}", client=mac, level=30)
            return [(reply, self._destination(packet, MessageType.NAK))]

        if message_type == MessageType.RELEASE:
            pool.release(mac)
            self.events.emit("dhcp", "release", "RELEASE", client=mac)
            return []

        if message_type == MessageType.DECLINE:
            pool.decline(mac, packet.option_ip(Opt.REQUESTED_IP))
            self.events.emit("dhcp", "decline", "DECLINE", client=mac, level=30)
            return []

        if message_type == MessageType.INFORM:
            reply = packet.make_reply(MessageType.ACK, self.config.server_ip)
            reply.ciaddr = packet.ciaddr
            self._add_lease_options(reply, with_lease=False)
            return [(reply, self._destination(packet, MessageType.ACK))]
        return []

    def _handle_bootp(self, packet: DhcpPacket) -> List[Reply]:
        pool = self.config.pool
        assert pool is not None
        mac = packet.mac
        existing = pool.lookup(mac)
        ip = existing.ip if existing else pool.offer(mac, packet.ciaddr if int(packet.ciaddr) else None)
        if ip is None or not pool.confirm(mac, ip):
            self.events.emit("dhcp", "exhausted", "no address for BOOTP client", client=mac,
                             level=40)
            return []
        reply = DhcpPacket(op=BOOTREPLY, htype=packet.htype, hlen=packet.hlen, xid=packet.xid,
                           flags=packet.flags, giaddr=packet.giaddr, chaddr=packet.chaddr,
                           yiaddr=ip, siaddr=self.config.server_ip)
        reply.options[Opt.SUBNET_MASK] = self.config.subnet.netmask.packed
        if self.config.router:
            reply.options[Opt.ROUTER] = self.config.router.packed
        record = self.clients.get(mac)
        if record is not None:
            record.ip = str(ip)
        # Some firmware (the tray-loading iMac G3's) does BSDP LIST/SELECT, then
        # boots from a plain BOOTP reply's file field. So also send the booter
        # of the image this Mac selected, or the default image. The 10.0
        # kernel's own BOOTP only reads the mask and router and ignores this.
        image_note = "no boot file"
        arch = record.arch if record and record.arch else "ppc"
        image = None
        if record is not None and record.selected_image is not None:
            image = self.catalog.by_index(record.selected_image)
        if image is None or not image.supports(arch, None):
            image = self.catalog.default_for(arch, record.model if record else None)
        if image is not None:
            booter = image.booter_relpath(arch)
            root_path = root_path_for(image, self.config)
            if booter is not None and len(booter.encode()) <= 127:
                reply.file = booter.encode()
                if root_path:
                    reply.options[Opt.ROOT_PATH] = root_path.encode()
                image_note = f"booter {booter} ({image.name!r})"
        self.events.emit("dhcp", "bootp", f"BOOTP reply {ip}, {image_note}",
                         client=mac, ip=str(ip))
        return [(reply, self._destination(packet, MessageType.ACK))]

    def _add_lease_options(self, reply: DhcpPacket, with_lease: bool = True) -> None:
        reply.options[Opt.SUBNET_MASK] = self.config.subnet.netmask.packed
        if self.config.router:
            reply.options[Opt.ROUTER] = self.config.router.packed
        if self.config.dns:
            reply.options[Opt.DNS] = ip_list(self.config.dns)
        if with_lease:
            assert self.config.pool is not None
            lease = self.config.pool.lease_time
            reply.options[Opt.LEASE_TIME] = lease.to_bytes(4, "big")
            reply.options[Opt.RENEWAL_TIME] = (lease // 2).to_bytes(4, "big")
            reply.options[Opt.REBINDING_TIME] = (lease * 7 // 8).to_bytes(4, "big")

    def _maybe_netboot_1_0(self, reply: DhcpPacket, client: Optional[ClientClass],
                           bsdp_options: Dict[int, bytes], mac: str) -> None:
        """NetBoot 1.0 firmware expects the default image's boot file in the DHCP reply."""
        if client is None or BsdpTag.NETBOOT_1_0_FIRMWARE not in bsdp_options:
            return
        image = self.catalog.default_for(client.arch, client.model)
        if image is None:
            self.events.emit("bsdp", "nb10-none", "NetBoot 1.0 client but no default image",
                             client=mac, level=30)
            return
        booter = image.booter_relpath(client.arch)
        root_path = root_path_for(image, self.config)
        if booter is None or root_path is None or len(booter.encode()) > 127:
            self.events.emit("bsdp", "nb10-failed",
                             f"NetBoot 1.0 client: image {image.name!r} cannot be served",
                             client=mac, level=40)
            return
        reply.siaddr = self.config.server_ip
        reply.file = booter.encode()
        reply.options[Opt.VENDOR_CLASS] = bsdp.VENDOR_CLASS.encode()
        reply.options[Opt.ROOT_PATH] = root_path.encode()
        self.events.emit("bsdp", "nb10", f"NetBoot 1.0 firmware: sending {image.name!r}",
                         client=mac, image=image.index)

    def _destination(self, packet: DhcpPacket, reply_type: MessageType) -> Destination:
        if int(packet.giaddr):
            return (str(packet.giaddr), self.config.server_port)
        if int(packet.ciaddr) and reply_type != MessageType.NAK:
            return (str(packet.ciaddr), self.config.client_port)
        # The client has no address yet. Broadcast, since without ARP or raw
        # sockets we can't unicast to it.
        return (self.config.broadcast_address, self.config.client_port)
