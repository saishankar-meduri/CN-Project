#!/usr/bin/env python3
r"""Triangle topology: 3 OpenFlow 1.3 switches, 6 hosts, remote Ryu controller.

        s1 ------ s2
         \        /
          \      /
            s3

h1,h2 -> s1   h3,h4 -> s2   h5,h6 -> s3
Switch-switch links are limited to 10 Mbps so floods cause real congestion.
A NAT node (root namespace, 10.0.0.254) lets hosts send UDP telemetry to the
controller machine: agents send to 10.0.0.254:9999.

Run:  sudo python3 topo.py
Extra CLI commands: agents start|stop, fault ... (see faults/faults.py)
"""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "faults"))

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.topo import Topo
from mininet.log import setLogLevel, info
from faults import FaultCLI


def write_runtime_manifest(net):
    """Publish this run's namespace owners for bounded out-of-band cleanup."""
    path = os.environ.get("SDN_RUNTIME_STATE_FILE")
    if not path:
        return
    state = {
        "run_token": os.environ.get("SDN_RUN_TOKEN"),
        "topology_pid": os.getpid(),
        "process_group_id": os.getpgrp(),
        "manifest_path": path,
        "host_pids": {node.name: node.pid for node in net.hosts if node.pid},
        "switch_pids": {node.name: node.pid for node in net.switches if node.pid},
        "agents": {},
        "iperf": [],
        "active_qdiscs": [],
        "downed_links": [],
    }
    temp_path = path + ".tmp.%d" % os.getpid()
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, path)


class TriangleTopo(Topo):
    def build(self):
        s1, s2, s3 = [self.addSwitch(f"s{i}", protocols="OpenFlow13") for i in (1, 2, 3)]
        hosts = [self.addHost(f"h{i}") for i in range(1, 7)]

        for h, s in zip(hosts, [s1, s1, s2, s2, s3, s3]):
            self.addLink(h, s)

        self.addLink(s1, s2, cls=TCLink, bw=10)
        self.addLink(s2, s3, cls=TCLink, bw=10)
        self.addLink(s1, s3, cls=TCLink, bw=10)


def run():
    net = Mininet(
        topo=TriangleTopo(),
        controller=None,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
    )
    try:
        net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)
        nat = net.addNAT(connect="s1", ip="10.0.0.254/8")
        net.start()
        nat.configDefault()
        write_runtime_manifest(net)
        info("*** Telemetry collector address for agents: 10.0.0.254:9999\n")
        FaultCLI(net)
    finally:
        net.stop()
        print("MININET_NET_STOPPED", flush=True)


if __name__ == "__main__":
    setLogLevel("info")
    run()
