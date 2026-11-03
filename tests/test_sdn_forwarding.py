"""Focused unit tests for SDN controller MAC learning and forwarding."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

# Ryu 4.34's Eventlet dependency requires its legacy greendns integration to be
# disabled with current dnspython versions.
os.environ.setdefault("EVENTLET_NO_GREENDNS", "yes")
import eventlet.wsgi

if not hasattr(eventlet.wsgi, "ALREADY_HANDLED"):
    eventlet.wsgi.ALREADY_HANDLED = object()

from controller import sdn_controller


class FakeParser:
    @staticmethod
    def OFPActionOutput(port, *args):
        return ("output", port)

    @staticmethod
    def OFPMatch(**fields):
        return fields

    @staticmethod
    def OFPPacketOut(**fields):
        return fields


class FakeDatapath:
    def __init__(self, dpid):
        self.id = dpid
        self.ofproto = SimpleNamespace(
            OFPP_FLOOD=0xFFFFFFFB,
            OFP_NO_BUFFER=0xFFFFFFFF,
        )
        self.ofproto_parser = FakeParser()
        self.send_msg = Mock()


class ForwardingTests(unittest.TestCase):
    def setUp(self):
        self.controller = sdn_controller.SDNController.__new__(sdn_controller.SDNController)
        self.controller.mac_to_port = {}
        self.controller.mac_to_host = {
            meta["mac"]: host for host, meta in sdn_controller.KNOWN_HOSTS.items()
        }
        self.controller.dropped_macs = {}
        self.controller.log_event = Mock()
        self.controller.add_flow = Mock()
        self.datapath = FakeDatapath(1)

    def send_unicast(self, *, in_port, src, dst, dpid=1, ethertype=0x0800):
        eth = SimpleNamespace(
            ethertype=ethertype,
            src=src,
            dst=dst,
        )
        datapath = FakeDatapath(dpid)
        msg = SimpleNamespace(
            datapath=datapath,
            match={"in_port": in_port},
            data=b"packet",
            buffer_id=datapath.ofproto.OFP_NO_BUFFER,
        )
        with patch.object(sdn_controller.packet, "Packet") as packet_cls:
            packet_cls.return_value.get_protocols.return_value = [eth]
            self.controller.packet_in_handler(SimpleNamespace(msg=msg))
        return datapath.send_msg.call_args.args[0]["actions"][0][1]

    def send_broadcast(
        self,
        *,
        dpid,
        in_port,
        src,
        data=b"identical-arp",
        dst="ff:ff:ff:ff:ff:ff",
    ):
        datapath = FakeDatapath(dpid)
        eth = SimpleNamespace(
            ethertype=0x0806,
            src=src,
            dst=dst,
        )
        msg = SimpleNamespace(
            datapath=datapath,
            match={"in_port": in_port},
            data=data,
            buffer_id=datapath.ofproto.OFP_NO_BUFFER,
        )
        with patch.object(sdn_controller.packet, "Packet") as packet_cls:
            packet_cls.return_value.get_protocols.return_value = [eth]
            self.controller.packet_in_handler(SimpleNamespace(msg=msg))
        return datapath

    def test_h2_to_h1_uses_s1_port_1(self):
        self.assertEqual(
            self.send_unicast(in_port=2, src="00:00:00:00:00:02", dst="00:00:00:00:00:01"),
            1,
        )

    def test_h1_to_h2_uses_s1_port_2(self):
        self.assertEqual(
            self.send_unicast(in_port=1, src="00:00:00:00:00:01", dst="00:00:00:00:00:02"),
            2,
        )

    def test_h1_to_h3_uses_s1_port_3(self):
        self.assertEqual(
            self.send_unicast(in_port=1, src="00:00:00:00:00:01", dst="00:00:00:00:00:03"),
            3,
        )

    def test_h1_to_h5_uses_s1_port_4(self):
        self.assertEqual(
            self.send_unicast(in_port=1, src="00:00:00:00:00:01", dst="00:00:00:00:00:05"),
            4,
        )

    def test_wrong_learned_known_destination_cannot_override_static_mapping(self):
        self.controller.mac_to_port[1] = {"00:00:00:00:00:01": 4}
        out_port = self.send_unicast(
            in_port=2,
            src="00:00:00:00:00:02",
            dst="00:00:00:00:00:01",
        )
        self.assertEqual(out_port, 1)

    def test_wrong_ingress_for_known_source_is_logged_and_not_learned(self):
        self.controller.mac_to_port[1] = {"00:00:00:00:00:01": 1}
        self.send_unicast(
            in_port=5,
            src="00:00:00:00:00:01",
            dst="00:00:00:00:00:99",
        )
        self.assertEqual(self.controller.mac_to_port[1]["00:00:00:00:00:01"], 1)
        self.controller.log_event.assert_called_once()
        warning = self.controller.log_event.call_args.args[0]
        self.assertIn("00:00:00:00:00:01", warning)
        self.assertIn("expected DPID 1/port 1", warning)
        self.assertIn("observed DPID 1/port 5", warning)
        self.assertEqual(self.controller.log_event.call_args.kwargs["level"], "WARNING")

    def test_known_host_transit_source_is_not_learned_or_flagged(self):
        self.send_unicast(
            dpid=2,
            in_port=3,
            src="00:00:00:00:00:01",
            dst="00:00:00:00:00:99",
        )
        self.assertNotIn("00:00:00:00:00:01", self.controller.mac_to_port[2])
        self.controller.log_event.assert_not_called()

    def test_unknown_destination_uses_learning_then_flooding(self):
        unknown_src = "00:00:00:00:aa:01"
        unknown_dst = "00:00:00:00:aa:02"
        flood_port = self.datapath.ofproto.OFPP_FLOOD
        self.assertEqual(
            self.send_unicast(in_port=6, src=unknown_src, dst=unknown_dst),
            flood_port,
        )
        self.assertEqual(self.controller.mac_to_port[1][unknown_src], 6)

        self.controller.mac_to_port[1][unknown_dst] = 7
        self.assertEqual(
            self.send_unicast(in_port=6, src=unknown_src, dst=unknown_dst),
            7,
        )

    def test_identical_arp_broadcast_is_processed_on_s1_and_s2(self):
        data = b"same-arp-frame"
        s1 = self.send_broadcast(
            dpid=1, in_port=1, src="00:00:00:00:00:01", data=data
        )
        s2 = self.send_broadcast(
            dpid=2, in_port=3, src="00:00:00:00:00:01", data=data
        )
        self.assertEqual(s1.send_msg.call_count, 1)
        self.assertEqual(s2.send_msg.call_count, 1)
        self.assertEqual(s1.send_msg.call_args.args[0]["actions"], [("output", s1.ofproto.OFPP_FLOOD)])
        self.assertEqual(
            {action[1] for action in s2.send_msg.call_args.args[0]["actions"]},
            {1, 2},
        )

    def test_inter_switch_broadcast_is_sent_only_to_local_host_ports(self):
        datapath = self.send_broadcast(
            dpid=2, in_port=3, src="00:00:00:00:00:01"
        )
        actions = datapath.send_msg.call_args.args[0]["actions"]
        self.assertEqual({action[1] for action in actions}, {1, 2})

    def test_host_originated_broadcast_uses_switch_flood(self):
        datapath = self.send_broadcast(
            dpid=1, in_port=1, src="00:00:00:00:00:01"
        )
        actions = datapath.send_msg.call_args.args[0]["actions"]
        self.assertEqual(actions, [("output", datapath.ofproto.OFPP_FLOOD)])

    def test_inter_switch_broadcast_is_not_reflooded_onto_triangle_links(self):
        for dpid, in_port in ((1, 3), (1, 4), (2, 3), (2, 4), (3, 3), (3, 4)):
            datapath = self.send_broadcast(
                dpid=dpid,
                in_port=in_port,
                src="00:00:00:00:aa:01",
                data=f"frame-{dpid}-{in_port}".encode(),
            )
            out_ports = {action[1] for action in datapath.send_msg.call_args.args[0]["actions"]}
            inter_switch_ports = {
                port
                for (switch_dpid, _peer_dpid), port in sdn_controller.INTER_SWITCH_PORTS.items()
                if switch_dpid == dpid
            }
            self.assertTrue(out_ports.isdisjoint(inter_switch_ports))
            self.assertTrue(out_ports.issubset(sdn_controller.HOST_ACCESS_PORTS[dpid]))

    def test_ipv6_multicast_uses_host_flooding_and_inter_switch_split_horizon(self):
        multicast_mac = "33:33:ff:00:00:01"
        origin = self.send_broadcast(
            dpid=1,
            in_port=1,
            src="00:00:00:00:00:01",
            dst=multicast_mac,
        )
        self.assertEqual(
            origin.send_msg.call_args.args[0]["actions"],
            [("output", origin.ofproto.OFPP_FLOOD)],
        )

        transit = self.send_broadcast(
            dpid=2,
            in_port=3,
            src="00:00:00:00:00:01",
            dst=multicast_mac,
        )
        actions = transit.send_msg.call_args.args[0]["actions"]
        self.assertEqual({action[1] for action in actions}, {1, 2})
        self.assertNotIn(transit.ofproto.OFPP_FLOOD, {action[1] for action in actions})

    def test_nat_arp_request_and_host_replies_use_port_5(self):
        nat_mac = "00:00:00:00:00:07"
        for host_mac in ("00:00:00:00:00:01", "00:00:00:00:00:02"):
            with self.subTest(host_mac=host_mac):
                self.controller.mac_to_port = {}
                request = self.send_broadcast(
                    dpid=1, in_port=5, src=nat_mac, data=b"nat-arp-request"
                )
                self.assertEqual(
                    request.send_msg.call_args.args[0]["actions"],
                    [("output", request.ofproto.OFPP_FLOOD)],
                )
                self.assertEqual(self.controller.mac_to_port[1][nat_mac], 5)

                reply_port = self.send_unicast(
                    in_port=1 if host_mac.endswith("01") else 2,
                    src=host_mac,
                    dst=nat_mac,
                    ethertype=0x0806,
                )
                self.assertEqual(reply_port, 5)

    def test_host_arp_request_and_nat_replies_use_configured_host_ports(self):
        nat_mac = "00:00:00:00:00:07"
        for host_mac, host_port in (
            ("00:00:00:00:00:01", 1),
            ("00:00:00:00:00:02", 2),
        ):
            with self.subTest(host_mac=host_mac):
                self.controller.mac_to_port = {}
                request = self.send_broadcast(
                    dpid=1, in_port=host_port, src=host_mac, data=b"host-arp-request"
                )
                self.assertEqual(
                    request.send_msg.call_args.args[0]["actions"],
                    [("output", request.ofproto.OFPP_FLOOD)],
                )

                reply_port = self.send_unicast(
                    in_port=5,
                    src=nat_mac,
                    dst=host_mac,
                    ethertype=0x0806,
                )
                self.assertEqual(reply_port, host_port)


if __name__ == "__main__":
    unittest.main()
