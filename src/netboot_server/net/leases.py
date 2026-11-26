"""Address pool for full DHCP mode."""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Iterable, Optional, Set

if TYPE_CHECKING:
    from ..store import Store

log = logging.getLogger("netboot.dhcp")

OFFER_HOLD_SECONDS = 60
DECLINE_QUARANTINE_SECONDS = 600

IPv4 = ipaddress.IPv4Address


@dataclass
class Lease:
    ip: IPv4
    expires: float
    bound: bool


class LeasePool:
    def __init__(self, start: IPv4, end: IPv4, lease_time: int = 3600,
                 reservations: Optional[Dict[str, IPv4]] = None,
                 excluded: Iterable[IPv4] = (), store: Optional["Store"] = None,
                 clock=time.time) -> None:
        if int(end) < int(start):
            raise ValueError("pool end is before pool start")
        self.start, self.end = start, end
        self.lease_time = lease_time
        self.reservations = {mac.lower(): ip for mac, ip in (reservations or {}).items()}
        self.excluded: Set[IPv4] = set(excluded)
        self.store = store
        self._clock = clock
        self._leases: Dict[str, Lease] = {}
        self._quarantine: Dict[IPv4, float] = {}
        self._load()

    # -- queries -------------------------------------------------------------
    def in_pool(self, ip: IPv4) -> bool:
        return int(self.start) <= int(ip) <= int(self.end)

    def lookup(self, mac: str) -> Optional[Lease]:
        lease = self._leases.get(mac.lower())
        if lease and lease.expires > self._clock():
            return lease
        return None

    def mac_for(self, ip: IPv4) -> Optional[str]:
        """MAC of the client currently holding ``ip`` (bound, unexpired lease)."""
        now = self._clock()
        for mac, lease in self._leases.items():
            if lease.ip == ip and lease.bound and lease.expires > now:
                return mac
        return None

    def _available(self, ip: IPv4, mac: str) -> bool:
        now = self._clock()
        if ip in self.excluded or self._quarantine.get(ip, 0) > now:
            return False
        for other_mac, reserved_ip in self.reservations.items():
            if reserved_ip == ip and other_mac != mac:
                return False
        for other_mac, lease in self._leases.items():
            if other_mac != mac and lease.ip == ip and lease.expires > now:
                return False
        return True

    # -- state changes ---------------------------------------------------------
    def offer(self, mac: str, requested: Optional[IPv4] = None) -> Optional[IPv4]:
        mac = mac.lower()
        candidates = []
        if mac in self.reservations:
            candidates.append(self.reservations[mac])
        existing = self._leases.get(mac)
        if existing:
            candidates.append(existing.ip)
        if requested is not None and self.in_pool(requested):
            candidates.append(requested)
        for ip in candidates:
            if self._available(ip, mac):
                return self._hold(mac, ip)
        for value in range(int(self.start), int(self.end) + 1):
            ip = IPv4(value)
            if self._available(ip, mac):
                return self._hold(mac, ip)
        return None

    def _hold(self, mac: str, ip: IPv4) -> IPv4:
        existing = self._leases.get(mac)
        if existing and existing.ip == ip and existing.bound and existing.expires > self._clock():
            return ip  # renewing client keeps its binding
        self._leases[mac] = Lease(ip, self._clock() + OFFER_HOLD_SECONDS, bound=False)
        return ip

    def confirm(self, mac: str, ip: IPv4) -> bool:
        mac = mac.lower()
        reserved = self.reservations.get(mac)
        if reserved is not None and reserved != ip:
            return False
        if reserved is None and not self.in_pool(ip):
            return False
        if not self._available(ip, mac):
            return False
        lease = Lease(ip, self._clock() + self.lease_time, bound=True)
        self._leases[mac] = lease
        if self.store:
            self.store.save_lease(mac, str(ip), lease.expires)
        return True

    def release(self, mac: str) -> None:
        if self._leases.pop(mac.lower(), None) is not None and self.store:
            self.store.delete_lease(mac)

    def drop_offer(self, mac: str) -> None:
        lease = self._leases.get(mac.lower())
        if lease and not lease.bound:
            del self._leases[mac.lower()]

    def decline(self, mac: str, ip: Optional[IPv4]) -> None:
        if ip is not None:
            self._quarantine[ip] = self._clock() + DECLINE_QUARANTINE_SECONDS
        self.release(mac)

    # -- persistence -------------------------------------------------------------
    def _load(self) -> None:
        if not self.store:
            return
        for mac, (ip, expires) in self.store.leases().items():
            try:
                self._leases[mac] = Lease(IPv4(ip), expires, True)
            except ValueError as exc:
                log.warning("ignoring saved lease for %s: %s", mac, exc)
