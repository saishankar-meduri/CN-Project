import unittest

from faults import test_runner as runner

FAULT_TS = 1700000000.0
LINKDOWN = {"id": "linkdown", "probe": ("h1", "h4")}


def alert(offset, host, tag="HOST/LINK DOWN"):
    return ("[%.3f] [ANOMALY] ANOMALY DETECTED [%s] on %s: test line"
            % (FAULT_TS + offset, tag, host))


class HostFilterTests(unittest.TestCase):
    def test_alert_on_faulted_host_counts(self):
        detection, latency, _, _ = runner.parse_controller_anomaly(
            [alert(5, "h1")], FAULT_TS, LINKDOWN)
        self.assertIsNotNone(detection)
        self.assertAlmostEqual(latency, 5.0, places=1)

    def test_alert_on_other_host_does_not_count(self):
        detection, latency, _, _ = runner.parse_controller_anomaly(
            [alert(5, "h5")], FAULT_TS, LINKDOWN)
        self.assertIsNone(detection)
        self.assertIsNone(latency)

    def test_stale_alert_on_other_host_does_not_hide_real_detection(self):
        lines = [alert(1, "h5"), alert(9, "h1")]
        detection, latency, _, _ = runner.parse_controller_anomaly(
            lines, FAULT_TS, LINKDOWN)
        self.assertIsNotNone(detection)
        self.assertAlmostEqual(latency, 9.0, places=1)

    def test_scenario_without_probe_host_is_not_filtered(self):
        detection, _, _, _ = runner.parse_controller_anomaly(
            [alert(5, "h5")], FAULT_TS, {"id": "linkdown"})
        self.assertIsNotNone(detection)


if __name__ == "__main__":
    unittest.main()


# ---- "flows active" rule: only traffic from the host, or on its own port ----
import os
os.environ.setdefault("EVENTLET_NO_GREENDNS", "yes")
import eventlet.wsgi

if not hasattr(eventlet.wsgi, "ALREADY_HANDLED"):
    eventlet.wsgi.ALREADY_HANDLED = object()

from controller import sdn_controller

H1 = sdn_controller.KNOWN_HOSTS["h1"]


def flow(**match):
    return ("flow", "sig", tuple(match.items()))


class FlowsActiveTests(unittest.TestCase):
    def setUp(self):
        self.ctl = sdn_controller.SDNController.__new__(sdn_controller.SDNController)
        self.ctl.port_throughput = {}
        self.ctl.flow_throughput = {}

    def active(self):
        return self.ctl._check_flows_active("h1", H1["mac"], H1["dpid"], H1["port"])

    def test_no_traffic_is_inactive(self):
        self.assertFalse(self.active())

    def test_traffic_only_sent_to_host_is_inactive(self):
        # Peers keep probing a disconnected host; that is not proof it is alive.
        self.ctl.flow_throughput = {flow(eth_dst=H1["mac"]): 5000.0}
        self.assertFalse(self.active())

    def test_traffic_sent_by_host_is_active(self):
        self.ctl.flow_throughput = {flow(eth_src=H1["mac"]): 5000.0}
        self.assertTrue(self.active())

    def test_access_port_traffic_is_active(self):
        self.ctl.port_throughput = {(H1["dpid"], H1["port"]): {"max_bps": 5000.0}}
        self.assertTrue(self.active())

    def test_traffic_below_threshold_is_inactive(self):
        self.ctl.flow_throughput = {flow(eth_src=H1["mac"]): 500.0}
        self.assertFalse(self.active())
