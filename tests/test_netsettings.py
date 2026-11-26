"""Network settings: parsing, command line over saved, and the checks before applying."""

import ipaddress
import unittest

from simplenetinstallppc.netif import Interface
from simplenetinstallppc.netsettings import NetworkSettings, SettingsError, check, merge

IP = ipaddress.IPv4Address
EN7 = Interface("en7", mac="84:ba:59:00:00:d4", up=True,
                addresses=[ipaddress.IPv4Interface("192.168.50.100/24"),
                           ipaddress.IPv4Interface("10.0.0.1/24")])
INTERFACES = [Interface("lo0", up=True, addresses=[ipaddress.IPv4Interface("127.0.0.1/8")]), EN7,
              Interface("en8", mac="00:11:22:33:44:55")]


def full(**extra):
    values = dict(interface="en7", dhcp_mode="full", pool_start="192.168.50.50",
                  pool_end="192.168.50.59")
    values.update(extra)
    return NetworkSettings.from_json(values)


class ParseTests(unittest.TestCase):
    def test_round_trip(self):
        settings = full(router="192.168.50.1", dns=["192.168.50.1", "8.8.8.8"], lease_time=600)
        again = NetworkSettings.from_json(settings.to_json())
        self.assertEqual(again, settings)
        self.assertEqual(settings.to_json()["dns"], ["192.168.50.1", "8.8.8.8"])

    def test_blank_fields_are_unset(self):
        settings = NetworkSettings.from_json({"interface": "", "router": "", "dns": "",
                                              "server_ip": None})
        self.assertEqual(settings, NetworkSettings())

    def test_bad_values(self):
        for bad in ({"router": "192.168.1"}, {"dns": "8.8.8.8, nope"}, {"dhcp_mode": "dhcp"},
                    {"dhcp_clients": "some"}, {"lease_time": 5}, {"lease_time": "soon"},
                    {"interface": ["en0"]}):
            with self.assertRaises(SettingsError, msg=bad):
                NetworkSettings.from_json(bad)


class MergeTests(unittest.TestCase):
    def test_command_line_wins_over_saved(self):
        saved = full(router="192.168.50.1")
        merged = merge(saved, {"dhcp_mode": "bsdp-only", "interface": "en8"})
        self.assertEqual((merged.interface, merged.dhcp_mode, merged.router),
                         ("en8", "bsdp-only", IP("192.168.50.1")))

    def test_nothing_saved(self):
        self.assertEqual(merge(None, {}), NetworkSettings())


class CheckTests(unittest.TestCase):
    def test_good(self):
        iface, address = check(full(), INTERFACES)
        self.assertEqual((iface.name, str(address)), ("en7", "192.168.50.100/24"))
        _, address = check(full(server_ip="10.0.0.1", pool_start="10.0.0.50",
                                pool_end="10.0.0.60"), INTERFACES)
        self.assertEqual(str(address), "10.0.0.1/24")
        check(NetworkSettings(interface="en7"), INTERFACES)       # BSDP only needs no pool

    def test_refused(self):
        cases = {
            "choose the network interface": NetworkSettings(),
            "no network interface en9": NetworkSettings(interface="en9"),
            "no IPv4 address": NetworkSettings(interface="en8"),
            "not configured on en7": full(server_ip="10.0.0.9"),
            "needs the first and last": NetworkSettings.from_json({"interface": "en7",
                                                                   "dhcp_mode": "full"}),
            "outside 192.168.50.0/24": full(pool_end="192.168.51.10"),
            "first address is after": full(pool_start="192.168.50.60"),
            "own address": full(pool_end="192.168.50.120"),
            "router 10.0.0.1 is outside": full(router="10.0.0.1"),
        }
        for message, settings in cases.items():
            with self.assertRaises(SettingsError, msg=message) as caught:
                check(settings, INTERFACES)
            self.assertIn(message, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
