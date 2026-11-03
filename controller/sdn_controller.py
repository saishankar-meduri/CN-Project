#!/usr/bin/env python3
"""Distributed Network Monitoring SDN Controller (OpenFlow 1.3).

This Ryu application implements an intelligent OpenFlow 1.3 SDN controller for a
distributed network-monitoring system. It combines host application telemetry
received over UDP sockets with switch network-flow statistics to detect and
dynamically mitigate network anomalies.

Core Subsystems:
1. OpenFlow 1.3 Learning Switch:
   - Full MAC address learning with packet-in handling.
   - Flow rule installation (reactive forwarding).
   - Loop-free broadcast handling for the triangle topology.
2. Threaded UDP Telemetry Collector:
   - Listens on UDP port 9999 for host telemetry JSON datagrams.
   - Handles malformed JSON payloads gracefully.
   - Detects lost datagrams via sequence number tracking (seq gap > 1).
   - Maintains real-time telemetry state per host.
3. Periodic Flow and Port Statistics Polling:
   - Queries connected switches every 5 seconds via OFPFlowStatsRequest and
     OFPPortStatsRequest.
   - Computes delta throughput (bps) per flow and per port.
4. Correlation and Anomaly Detection Engine:
   - Rule 1: High latency (>100ms) + High throughput (>8Mbps) -> CONGESTION/FLOOD
   - Rule 2: High latency (>100ms) + Normal throughput (<=8Mbps) -> HOST/APP PROBLEM
   - Rule 3: Missing telemetry (>5s) + Active flows -> AGENT CRASH
   - Rule 4: Missing telemetry (>5s) + No flows -> HOST/LINK DOWN
5. Dynamic Mitigation Engine:
   - Baseline mode: detection and logging only.
   - Dynamic mode: installs drop rules for flooders (priority 200, 30s timeout),
     reroutes flows around congested/down links via alternate triangle paths,
     and deletes stale flows.
6. Persistent & Console Logging:
   - Logs all anomalies, actions, and timestamps to logs/controller.log and stdout.

Usage:
    ryu-manager controller/sdn_controller.py
    ryu-manager controller/sdn_controller.py --mode dynamic
"""

import datetime
import json
import os
import socket
import sys
import threading
import time

from ryu import cfg
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER,
    DEAD_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from ryu.lib import hub
from ryu.lib.packet import (
    arp,
    ether_types,
    ethernet,
    ipv4,
    packet,
    tcp,
    udp,
)
from ryu.ofproto import ofproto_v1_3

# -----------------------------------------------------------------------------
# Configuration Options
# -----------------------------------------------------------------------------
CONF = cfg.CONF
try:
    CONF.register_opt(
        cfg.StrOpt(
            "mode",
            default="baseline",
            help="Controller operation mode: 'baseline' (detection only) or 'dynamic' (active mitigation)",
        )
    )
except cfg.DuplicateOptError:
    pass

# -----------------------------------------------------------------------------
# Global Constants & Topology Definitions
# -----------------------------------------------------------------------------
DEFAULT_TELEMETRY_PORT = 9999
STATS_POLL_INTERVAL_SEC = 5.0
TELEMETRY_TIMEOUT_SEC = 5.0
CONGESTION_LATENCY_THRESHOLD_MS = 100.0
CONGESTION_THROUGHPUT_THRESHOLD_BPS = 8_000_000.0  # 8 Mbps on 10 Mbps bottleneck

# Flow Priorities
PRIORITY_TABLE_MISS = 0
PRIORITY_NORMAL_FORWARDING = 1
PRIORITY_REROUTE = 150
PRIORITY_DROP_OFFENDER = 200

# Timeouts
TIMEOUT_DROP_HARD_SEC = 30
TIMEOUT_REROUTE_HARD_SEC = 60
TIMEOUT_NORMAL_IDLE_SEC = 20
TIMEOUT_NORMAL_HARD_SEC = 100

# Topology mapping for the 3-switch triangle
# s1 <-> s2 (s1:3 <-> s2:3)
# s2 <-> s3 (s2:4 <-> s3:3)
# s1 <-> s3 (s1:4 <-> s3:4)
KNOWN_HOSTS = {
    "h1": {"mac": "00:00:00:00:00:01", "ip": "10.0.0.1", "dpid": 1, "port": 1},
    "h2": {"mac": "00:00:00:00:00:02", "ip": "10.0.0.2", "dpid": 1, "port": 2},
    "h3": {"mac": "00:00:00:00:00:03", "ip": "10.0.0.3", "dpid": 2, "port": 1},
    "h4": {"mac": "00:00:00:00:00:04", "ip": "10.0.0.4", "dpid": 2, "port": 2},
    "h5": {"mac": "00:00:00:00:00:05", "ip": "10.0.0.5", "dpid": 3, "port": 1},
    "h6": {"mac": "00:00:00:00:00:06", "ip": "10.0.0.6", "dpid": 3, "port": 2},
}

# Switch Interconnect Map: (dpid, peer_dpid) -> egress_port
INTER_SWITCH_PORTS = {
    (1, 2): 3,
    (1, 3): 4,
    (2, 1): 3,
    (2, 3): 4,
    (3, 2): 3,
    (3, 1): 4,
}

# Host access ports on each switch (including NAT at s1:5)
HOST_ACCESS_PORTS = {
    1: {1, 2, 5},
    2: {1, 2},
    3: {1, 2},
}

# Triangle topology alternate detour paths: (dpid_a, dpid_b) -> transit_dpid
TRIANGLE_DETOUR = {
    (1, 2): 3,
    (2, 1): 3,
    (2, 3): 1,
    (3, 2): 1,
    (1, 3): 2,
    (3, 1): 2,
}


class SDNController(app_manager.RyuApp):
    """OpenFlow 1.3 SDN Controller for Distributed Network Monitoring."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SDNController, self).__init__(*args, **kwargs)

        # Mode configuration
        self.mode = getattr(CONF, "mode", "baseline").lower()

        # Paths and logging setup
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.log_file_path = os.path.join(base_dir, "logs", "controller.log")
        os.makedirs(os.path.dirname(self.log_file_path), exist_ok=True)

        # Core required state containers
        self.telemetry = {}      # host_id -> {"seq": int, "timestamp": float, "metrics": dict, "local_time": float}
        self.flow_stats = {}     # dpid -> {"timestamp": float, "flows": list}
        self.port_stats = {}     # dpid -> {port_no: dict}
        self.mac_to_port = {}    # dpid -> {mac: port}

        # Runtime network & topology state
        self.datapaths = {}      # dpid -> datapath
        self.last_seq = {}       # host_id -> int
        self.prev_flow_stats = {}  # (dpid, flow_cookie_or_match) -> {"byte_count": int, "time": float}
        self.prev_port_stats = {}  # (dpid, port_no) -> {"rx_bytes": int, "tx_bytes": int, "time": float}
        self.port_throughput = {}  # (dpid, port_no) -> {"rx_bps": float, "tx_bps": float, "max_bps": float}
        self.flow_throughput = {}  # flow_signature -> bps
        self.seen_broadcasts = {}  # packet_hash -> timestamp (loop prevention)
        self.dropped_macs = {}     # mac -> unblock_timestamp
        self.active_reroutes = set()  # set of rerouted link pairs

        # Reverse lookups for hosts
        self.mac_to_host = {meta["mac"]: hid for hid, meta in KNOWN_HOSTS.items()}
        self.ip_to_host = {meta["ip"]: hid for hid, meta in KNOWN_HOSTS.items()}

        self.log_event(
            f"SDN Controller initialized. Mode: [{self.mode.upper()}], "
            f"Telemetry Port: {DEFAULT_TELEMETRY_PORT}, Stats Interval: {STATS_POLL_INTERVAL_SEC}s",
            level="STARTUP",
        )

        # Launch UDP Telemetry Collector Thread
        self.collector_running = True
        self.collector_thread = threading.Thread(
            target=self._run_udp_collector,
            args=(DEFAULT_TELEMETRY_PORT,),
            daemon=True,
            name="UDPTelemetryCollector",
        )
        self.collector_thread.start()

        # Launch periodic stats polling & anomaly detection loop
        self.monitor_thread = hub.spawn(self._monitor_loop)

    # -------------------------------------------------------------------------
    # Logging System
    # -------------------------------------------------------------------------
    def log_event(self, message: str, level: str = "INFO"):
        """Logs an event with timestamp both to the console and logs/controller.log."""
        now = time.time()
        time_str = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        formatted = f"[{time_str}] [{now:.3f}] [{level}] {message}"

        # Print to console immediately
        print(formatted, flush=True)

        # Write to log file
        try:
            with open(self.log_file_path, "a", encoding="utf-8") as f:
                f.write(formatted + "\n")
        except OSError as err:
            self.logger.error("Failed to append to log file %s: %s", self.log_file_path, err)

    # -------------------------------------------------------------------------
    # Threaded UDP Telemetry Collector
    # -------------------------------------------------------------------------
    def _run_udp_collector(self, port: int):
        """Threaded UDP server that collects telemetry datagrams from hosts."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            sock.bind(("0.0.0.0", port))
            self.log_event(f"UDP Telemetry Collector bound to 0.0.0.0:{port}", level="COLLECTOR")
        except OSError as err:
            self.log_event(f"Failed to bind UDP socket to port {port}: {err}", level="ERROR")
            return

        sock.settimeout(1.0)
        while self.collector_running:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as err:
                if self.collector_running:
                    self.log_event(f"Socket error in telemetry collector: {err}", level="ERROR")
                break

            self._process_telemetry_datagram(data, addr)

        sock.close()
        self.log_event("UDP Telemetry Collector stopped.", level="COLLECTOR")

    def _process_telemetry_datagram(self, raw_data: bytes, addr):
        """Validates, parses, tracks sequence continuity, and stores telemetry."""
        now = time.time()
        try:
            payload = raw_data.decode("utf-8")
            record = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            self.log_event(
                f"MALFORMED_JSON: Received invalid datagram from {addr[0]}:{addr[1]} -> {err}",
                level="WARNING",
            )
            return

        # Validate mandatory schema fields
        if not isinstance(record, dict):
            self.log_event(f"INVALID_TELEMETRY: Non-dict payload received from {addr}: {record}", level="WARNING")
            return

        required_keys = {"host_id", "seq", "timestamp", "metrics"}
        missing_keys = required_keys - set(record.keys())
        if missing_keys:
            self.log_event(
                f"INVALID_TELEMETRY: Missing required keys {missing_keys} from {addr}: {record}",
                level="WARNING",
            )
            return

        host_id = str(record["host_id"])
        try:
            seq = int(record["seq"])
            orig_ts = float(record["timestamp"])
            metrics = record["metrics"]
            if not isinstance(metrics, dict):
                raise ValueError("metrics must be a dictionary")
        except (ValueError, TypeError) as err:
            self.log_event(f"INVALID_TELEMETRY: Field type error in {record} from {addr}: {err}", level="WARNING")
            return

        # Sequence gap detection (if seq gap > 1, log warning)
        if host_id in self.last_seq:
            prev_seq = self.last_seq[host_id]
            seq_diff = seq - prev_seq
            if seq_diff > 1:
                lost_count = seq_diff - 1
                self.log_event(
                    f"SEQUENCE_GAP: Host '{host_id}' sequence jump from {prev_seq} to {seq} "
                    f"(seq gap = {seq_diff}, detected {lost_count} lost datagrams)",
                    level="WARNING",
                )
            elif seq_diff < 0:
                self.log_event(
                    f"SEQUENCE_OUT_OF_ORDER: Host '{host_id}' received old seq {seq} after {prev_seq}",
                    level="INFO",
                )
        self.last_seq[host_id] = seq

        # Store latest telemetry record
        self.telemetry[host_id] = {
            "seq": seq,
            "timestamp": orig_ts,
            "metrics": metrics,
            "local_time": now,
            "src_ip": addr[0],
        }

    # -------------------------------------------------------------------------
    # OpenFlow Switch Features & State Management
    # -------------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Performs initial handshake and installs default table-miss flow rule."""
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id

        self.datapaths[dpid] = datapath
        self.mac_to_port.setdefault(dpid, {})

        # Table-miss flow entry: sends unmatched packets to the controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(
            datapath=datapath,
            priority=PRIORITY_TABLE_MISS,
            match=match,
            actions=actions,
        )

        self.log_event(
            f"Switch connected: s{dpid} (DPID={dpid}). Table-miss entry installed.",
            level="OFP_CONNECT",
        )

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        """Tracks active and disconnected datapaths."""
        datapath = ev.datapath
        dpid = datapath.id
        if ev.state == MAIN_DISPATCHER:
            if dpid not in self.datapaths:
                self.datapaths[dpid] = datapath
                self.log_event(f"Datapath registered: s{dpid} in MAIN_DISPATCHER", level="OFP_STATE")
        elif ev.state == DEAD_DISPATCHER:
            if dpid in self.datapaths:
                del self.datapaths[dpid]
                self.log_event(f"Datapath unregistered: s{dpid} disconnected", level="OFP_STATE")

    # -------------------------------------------------------------------------
    # Flow Table Modification Helpers
    # -------------------------------------------------------------------------
    def add_flow(
        self,
        datapath,
        priority: int,
        match,
        actions: list,
        buffer_id: int = None,
        idle_timeout: int = 0,
        hard_timeout: int = 0,
    ):
        """Installs a flow entry into an OpenFlow 1.3 switch table."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id is not None and buffer_id != ofproto.OFP_NO_BUFFER:
            mod = parser.OFPFlowMod(
                datapath=datapath,
                buffer_id=buffer_id,
                priority=priority,
                match=match,
                instructions=inst,
                idle_timeout=idle_timeout,
                hard_timeout=hard_timeout,
            )
        else:
            mod = parser.OFPFlowMod(
                datapath=datapath,
                priority=priority,
                match=match,
                instructions=inst,
                idle_timeout=idle_timeout,
                hard_timeout=hard_timeout,
            )
        datapath.send_msg(mod)

    def delete_flow(self, datapath, match=None, priority: int = None, out_port: int = None):
        """Deletes flow rules matching the specified criteria."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = match if match is not None else parser.OFPMatch()
        out_port = out_port if out_port is not None else ofproto.OFPP_ANY
        out_group = ofproto.OFPG_ANY
        priority = priority if priority is not None else 0

        mod = parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_DELETE,
            out_port=out_port,
            out_group=out_group,
            match=match,
            priority=priority,
        )
        datapath.send_msg(mod)

    # -------------------------------------------------------------------------
    # Packet-In & Learning Switch Forwarding
    # -------------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """Handles OpenFlow Packet-In messages for MAC learning and packet dispatch."""
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"]
        dpid = datapath.id

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        # Ignore LLDP control traffic
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst = eth.dst
        src = eth.src

        # Check if source MAC is currently suppressed by a drop rule
        now = time.time()
        if src in self.dropped_macs:
            if now < self.dropped_macs[src]:
                # Suppressed offender: discard packet-in
                return
            else:
                del self.dropped_macs[src]
                self.log_event(
                    f"Temporary drop timeout expired for MAC {src}. Resuming standard processing.",
                    level="RECOVERY",
                )

        # Standard MAC learning on the current switch
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port

        # Broadcast & Multicast storm prevention in the triangle topology
        is_broadcast = (dst == "ff:ff:ff:ff:ff:ff") or dst.startswith("01:00:5e")
        if is_broadcast:
            # Deduplicate broadcast datagrams across the triangle loop
            pkt_hash = hash((src, dst, eth.ethertype, msg.data[:64]))
            if pkt_hash in self.seen_broadcasts:
                if now - self.seen_broadcasts[pkt_hash] < 2.0:
                    # Drop duplicate broadcast packet circulating the triangle
                    return
            self.seen_broadcasts[pkt_hash] = now
            if len(self.seen_broadcasts) > 2000:
                self.seen_broadcasts = {k: v for k, v in self.seen_broadcasts.items() if now - v < 2.0}

            # Forward broadcast intelligently:
            # If arriving on an inter-switch link, deliver ONLY to local host ports
            if in_port in {3, 4}:
                target_ports = [p for p in HOST_ACCESS_PORTS.get(dpid, set()) if p != in_port]
                actions = [parser.OFPActionOutput(p) for p in target_ports]
            else:
                actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]

            data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
            out = parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=msg.buffer_id,
                in_port=in_port,
                actions=actions,
                data=data,
            )
            datapath.send_msg(out)
            return

        # Unicast Forwarding
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            # If destination is a known host, route towards the appropriate switch
            if dst in self.mac_to_host:
                target_host = self.mac_to_host[dst]
                target_dpid = KNOWN_HOSTS[target_host]["dpid"]
                if target_dpid == dpid:
                    out_port = KNOWN_HOSTS[target_host]["port"]
                else:
                    out_port = INTER_SWITCH_PORTS.get((dpid, target_dpid), ofproto.OFPP_FLOOD)
            else:
                out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        # Install unicast flow rule if destination is known and not flooding
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(
                    datapath=datapath,
                    priority=PRIORITY_NORMAL_FORWARDING,
                    match=match,
                    actions=actions,
                    buffer_id=msg.buffer_id,
                    idle_timeout=TIMEOUT_NORMAL_IDLE_SEC,
                    hard_timeout=TIMEOUT_NORMAL_HARD_SEC,
                )
                return
            else:
                self.add_flow(
                    datapath=datapath,
                    priority=PRIORITY_NORMAL_FORWARDING,
                    match=match,
                    actions=actions,
                    idle_timeout=TIMEOUT_NORMAL_IDLE_SEC,
                    hard_timeout=TIMEOUT_NORMAL_HARD_SEC,
                )

        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data,
        )
        datapath.send_msg(out)

    # -------------------------------------------------------------------------
    # Periodic Statistics Polling & Detection Loop
    # -------------------------------------------------------------------------
    def _monitor_loop(self):
        """Spawns every 5 seconds to query flow/port stats and run anomaly detection."""
        # Warmup delay so switches can finish initial handshake
        hub.sleep(2)
        while True:
            for dp in list(self.datapaths.values()):
                self._request_stats(dp)

            # Sleep half a second to let replies arrive before running correlation
            hub.sleep(0.5)

            # Run correlation and anomaly detection engine
            self._run_anomaly_detection()

            # Wait for next 5-second interval
            hub.sleep(STATS_POLL_INTERVAL_SEC - 0.5)

    def _request_stats(self, datapath):
        """Sends OFPFlowStatsRequest and OFPPortStatsRequest to a switch."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Flow stats request
        req_flow = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(req_flow)

        # Port stats request (all ports)
        req_port = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req_port)

    # -------------------------------------------------------------------------
    # Flow & Port Stats Reply Handlers
    # -------------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        """Processes flow statistics replies and calculates per-flow throughput."""
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        now = time.time()

        parsed_flows = []
        for stat in body:
            parsed_flows.append({
                "priority": stat.priority,
                "match": stat.match,
                "packet_count": stat.packet_count,
                "byte_count": stat.byte_count,
                "duration_sec": stat.duration_sec,
                "duration_nsec": stat.duration_nsec,
            })

            # Calculate flow bps
            match_dict = dict(stat.match.items())
            flow_sig = (dpid, stat.priority, tuple(sorted(match_dict.items())))
            if flow_sig in self.prev_flow_stats:
                prev = self.prev_flow_stats[flow_sig]
                dt = now - prev["time"]
                if dt > 0:
                    delta_bytes = max(0, stat.byte_count - prev["byte_count"])
                    bps = (delta_bytes * 8.0) / dt
                    self.flow_throughput[flow_sig] = bps

            self.prev_flow_stats[flow_sig] = {
                "byte_count": stat.byte_count,
                "packet_count": stat.packet_count,
                "time": now,
            }

        self.flow_stats[dpid] = {
            "timestamp": now,
            "flows": parsed_flows,
        }

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        """Processes port statistics replies and calculates per-port throughput."""
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        now = time.time()

        self.port_stats.setdefault(dpid, {})
        for stat in body:
            port_no = stat.port_no
            if port_no > ofproto_v1_3.OFPP_MAX:
                continue

            rx_bytes = stat.rx_bytes
            tx_bytes = stat.tx_bytes
            rx_packets = stat.rx_packets
            tx_packets = stat.tx_packets

            prev_key = (dpid, port_no)
            if prev_key in self.prev_port_stats:
                prev = self.prev_port_stats[prev_key]
                dt = now - prev["time"]
                if dt > 0:
                    rx_bps = (max(0, rx_bytes - prev["rx_bytes"]) * 8.0) / dt
                    tx_bps = (max(0, tx_bytes - prev["tx_bytes"]) * 8.0) / dt
                    self.port_throughput[prev_key] = {
                        "rx_bps": rx_bps,
                        "tx_bps": tx_bps,
                        "max_bps": max(rx_bps, tx_bps),
                        "timestamp": now,
                    }

            self.prev_port_stats[prev_key] = {
                "rx_bytes": rx_bytes,
                "tx_bytes": tx_bytes,
                "rx_packets": rx_packets,
                "tx_packets": tx_packets,
                "time": now,
            }

            self.port_stats[dpid][port_no] = {
                "rx_packets": rx_packets,
                "tx_packets": tx_packets,
                "rx_bytes": rx_bytes,
                "tx_bytes": tx_bytes,
                "rx_errors": stat.rx_errors,
                "tx_errors": stat.tx_errors,
                "timestamp": now,
            }

    # -------------------------------------------------------------------------
    # Anomaly Detection & Correlation Engine
    # -------------------------------------------------------------------------
    def _run_anomaly_detection(self):
        """Correlates telemetry data with OpenFlow statistics across all hosts.

        Detection Rules:
        - Rule 1: latency > 100ms AND throughput > 8 Mbps -> CONGESTION/FLOOD
        - Rule 2: latency > 100ms AND throughput normal   -> HOST/APP PROBLEM
        - Rule 3: telemetry missing > 5s AND flows active -> AGENT CRASH
        - Rule 4: telemetry missing > 5s AND no flows     -> HOST/LINK DOWN
        """
        now = time.time()

        for host_id, meta in KNOWN_HOSTS.items():
            mac = meta["mac"]
            dpid = meta["dpid"]
            port = meta["port"]

            telem = self.telemetry.get(host_id)
            is_telemetry_missing = False
            last_seen_time = 0.0

            if telem is None:
                is_telemetry_missing = True
            else:
                last_seen_time = telem.get("local_time", telem.get("timestamp", 0.0))
                if (now - last_seen_time) > TELEMETRY_TIMEOUT_SEC:
                    is_telemetry_missing = True

            # Determine whether network flows or port traffic are active for this host
            flows_active = self._check_flows_active(host_id, mac, dpid, port)

            if is_telemetry_missing:
                elapsed = now - last_seen_time if last_seen_time > 0 else 999.0
                if flows_active:
                    # Rule 3: Missing telemetry (>5s) AND active flows -> AGENT CRASH
                    self.log_event(
                        f"ANOMALY DETECTED [AGENT CRASH] on {host_id}: "
                        f"No telemetry received for {elapsed:.1f}s, but network traffic is actively flowing.",
                        level="ANOMALY",
                    )
                    if self.mode == "dynamic":
                        self._handle_dynamic_agent_crash(host_id)
                else:
                    # Rule 4: Missing telemetry (>5s) AND no active flows -> HOST/LINK DOWN
                    self.log_event(
                        f"ANOMALY DETECTED [HOST/LINK DOWN] on {host_id}: "
                        f"No telemetry for {elapsed:.1f}s and zero network activity detected.",
                        level="ANOMALY",
                    )
                    if self.mode == "dynamic":
                        self._handle_dynamic_host_link_down(host_id, mac, dpid)
            else:
                # Telemetry is present; inspect metrics
                metrics = telem.get("metrics", {})
                latency_ms = float(metrics.get("app_latency_ms", 0.0))

                # Determine effective throughput
                throughput_bps = self._get_host_throughput(mac, dpid, port)

                if latency_ms > CONGESTION_LATENCY_THRESHOLD_MS and throughput_bps > CONGESTION_THROUGHPUT_THRESHOLD_BPS:
                    # Rule 1: High latency (>100ms) AND very high throughput (>8 Mbps) -> CONGESTION/FLOOD
                    self.log_event(
                        f"ANOMALY DETECTED [CONGESTION/FLOOD] on {host_id}: "
                        f"App latency = {latency_ms:.2f} ms (> {CONGESTION_LATENCY_THRESHOLD_MS} ms), "
                        f"Throughput = {throughput_bps / 1e6:.2f} Mbps (> {CONGESTION_THROUGHPUT_THRESHOLD_BPS / 1e6:.1f} Mbps)",
                        level="ANOMALY",
                    )
                    if self.mode == "dynamic":
                        self._handle_dynamic_congestion(host_id, mac, dpid, throughput_bps)

                elif latency_ms > CONGESTION_LATENCY_THRESHOLD_MS and throughput_bps <= CONGESTION_THROUGHPUT_THRESHOLD_BPS:
                    # Rule 2: High latency (>100ms) AND throughput normal -> HOST/APP PROBLEM
                    self.log_event(
                        f"ANOMALY DETECTED [HOST/APP PROBLEM] on {host_id}: "
                        f"App latency = {latency_ms:.2f} ms (> {CONGESTION_LATENCY_THRESHOLD_MS} ms) "
                        f"with normal network throughput ({throughput_bps / 1e6:.2f} Mbps <= 8.0 Mbps). "
                        f"Likely application backlog, host netem delay, or host CPU load ({metrics.get('cpu_percent', 0.0)}%).",
                        level="ANOMALY",
                    )
                    # Rule 2 is log only in both baseline and dynamic modes

    # -------------------------------------------------------------------------
    # Correlation Support Functions
    # -------------------------------------------------------------------------
    def _check_flows_active(self, host_id: str, mac: str, dpid: int, port: int) -> bool:
        """Determines if there is active network traffic for a specific host."""
        # 1. Check access port throughput
        port_info = self.port_throughput.get((dpid, port))
        if port_info and port_info.get("max_bps", 0.0) > 1000.0:  # > 1 kbps
            return True

        # 2. Check flow entries matching the host's MAC address
        for flow_sig, bps in self.flow_throughput.items():
            if bps > 1000.0:
                _, _, match_items = flow_sig
                match_dict = dict(match_items)
                if match_dict.get("eth_src") == mac or match_dict.get("eth_dst") == mac:
                    return True

        # 3. Check raw packet counters in flow stats
        dp_flows = self.flow_stats.get(dpid, {}).get("flows", [])
        for f in dp_flows:
            match_dict = dict(f["match"].items())
            if match_dict.get("eth_src") == mac or match_dict.get("eth_dst") == mac:
                if f["packet_count"] > 0:
                    return True

        return False

    def _get_host_throughput(self, mac: str, dpid: int, port: int) -> float:
        """Calculates the max throughput observed for a host across port and flow tables."""
        rates = [0.0]

        # Port rate
        p_info = self.port_throughput.get((dpid, port))
        if p_info:
            rates.append(p_info.get("max_bps", 0.0))

        # Flow rates matching eth_src or eth_dst
        for flow_sig, bps in self.flow_throughput.items():
            _, _, match_items = flow_sig
            match_dict = dict(match_items)
            if match_dict.get("eth_src") == mac or match_dict.get("eth_dst") == mac:
                rates.append(bps)

        # Check inter-switch link rates connected to this switch
        for peer_dpid, is_port in INTER_SWITCH_PORTS.items():
            if peer_dpid[0] == dpid:
                link_info = self.port_throughput.get((dpid, is_port))
                if link_info:
                    rates.append(link_info.get("max_bps", 0.0))

        return max(rates)

    # -------------------------------------------------------------------------
    # Dynamic Mitigation Responses (Executed only in dynamic mode)
    # -------------------------------------------------------------------------
    def _handle_dynamic_congestion(self, host_id: str, mac: str, dpid: int, throughput_bps: float):
        """Mitigates congestion/flood: drops the offending source MAC and reroutes other flows."""
        # Find which host is the primary high-rate sender causing the flood
        offending_mac = mac
        highest_rate = throughput_bps

        for hid, hmeta in KNOWN_HOSTS.items():
            hmac = hmeta["mac"]
            hrate = self._get_host_throughput(hmac, hmeta["dpid"], hmeta["port"])
            if hrate > highest_rate:
                highest_rate = hrate
                offending_mac = hmac

        now = time.time()
        # Install drop rule for offending source MAC (priority 200, hard timeout 30s)
        for dp in self.datapaths.values():
            parser = dp.ofproto_parser
            match = parser.OFPMatch(eth_src=offending_mac)
            self.add_flow(
                datapath=dp,
                priority=PRIORITY_DROP_OFFENDER,
                match=match,
                actions=[],  # Empty actions = DROP
                hard_timeout=TIMEOUT_DROP_HARD_SEC,
            )

        self.dropped_macs[offending_mac] = now + TIMEOUT_DROP_HARD_SEC
        self.log_event(
            f"DYNAMIC ACTION [CONGESTION]: Installed DROP flow rule for offending MAC {offending_mac} "
            f"(priority={PRIORITY_DROP_OFFENDER}, hard_timeout={TIMEOUT_DROP_HARD_SEC}s) across all switches.",
            level="ACTION",
        )

        # Reroute other non-offending flows around the triangle bottleneck
        self._reroute_non_offending_flows(dpid, offending_mac)

    def _handle_dynamic_agent_crash(self, host_id: str):
        """Mitigates agent crash: logs high-priority alert while maintaining active flows."""
        self.log_event(
            f"DYNAMIC ACTION [AGENT CRASH]: High-priority alert dispatched for host '{host_id}'. "
            f"Network routing remains intact; telemetry agent restart required.",
            level="ACTION",
        )

    def _handle_dynamic_host_link_down(self, host_id: str, mac: str, dpid: int):
        """Mitigates host/link down: removes stale flow entries and installs reroute rules."""
        # 1. Delete stale flows for that host's MAC from all switches
        for dp in self.datapaths.values():
            parser = dp.ofproto_parser
            self.delete_flow(dp, match=parser.OFPMatch(eth_src=mac))
            self.delete_flow(dp, match=parser.OFPMatch(eth_dst=mac))

        self.log_event(
            f"DYNAMIC ACTION [HOST/LINK DOWN]: Purged stale flows for host '{host_id}' (MAC {mac}) "
            f"from all connected switches.",
            level="ACTION",
        )

        # 2. Flag and install alternate path reroutes in the triangle
        for peer_dpid in [d for d in (1, 2, 3) if d != dpid]:
            self._install_reroute_via_alternate_path(dpid, peer_dpid, exclude_mac=mac)

    # -------------------------------------------------------------------------
    # Triangle Topology Rerouting Engine
    # -------------------------------------------------------------------------
    def _reroute_non_offending_flows(self, src_dpid: int, exclude_mac: str):
        """Reroutes non-offending traffic between switches through the triangle alternate path."""
        for other_dpid in [d for d in (1, 2, 3) if d != src_dpid]:
            self._install_reroute_via_alternate_path(src_dpid, other_dpid, exclude_mac)

    def _install_reroute_via_alternate_path(self, dpid_a: int, dpid_b: int, exclude_mac: str = None):
        """Installs OpenFlow rules to detour traffic between switch A and B through transit switch C.

        Triangle topology mapping:
        - When direct link s1 <-> s2 is congested/down, route via s3.
        - When direct link s2 <-> s3 is congested/down, route via s1.
        - When direct link s1 <-> s3 is congested/down, route via s2.
        """
        transit_dpid = TRIANGLE_DETOUR.get((dpid_a, dpid_b))
        if not transit_dpid:
            return

        dp_a = self.datapaths.get(dpid_a)
        dp_b = self.datapaths.get(dpid_b)
        dp_transit = self.datapaths.get(transit_dpid)

        if not dp_a or not dp_b or not dp_transit:
            return

        port_a_to_transit = INTER_SWITCH_PORTS.get((dpid_a, transit_dpid))
        port_transit_to_b = INTER_SWITCH_PORTS.get((transit_dpid, dpid_b))
        port_b_to_transit = INTER_SWITCH_PORTS.get((dpid_b, transit_dpid))
        port_transit_to_a = INTER_SWITCH_PORTS.get((transit_dpid, dpid_a))

        # Target destination hosts attached to switch B
        target_hosts_b = [
            meta for hid, meta in KNOWN_HOSTS.items()
            if meta["dpid"] == dpid_b and (exclude_mac is None or meta["mac"] != exclude_mac)
        ]
        # Target destination hosts attached to switch A
        target_hosts_a = [
            meta for hid, meta in KNOWN_HOSTS.items()
            if meta["dpid"] == dpid_a and (exclude_mac is None or meta["mac"] != exclude_mac)
        ]

        # 1. On Switch A: route traffic destined to Hosts on B towards the Transit switch
        parser_a = dp_a.ofproto_parser
        for h in target_hosts_b:
            match = parser_a.OFPMatch(eth_dst=h["mac"])
            actions = [parser_a.OFPActionOutput(port_a_to_transit)]
            self.add_flow(
                datapath=dp_a,
                priority=PRIORITY_REROUTE,
                match=match,
                actions=actions,
                hard_timeout=TIMEOUT_REROUTE_HARD_SEC,
            )

        # 2. On Transit Switch: forward traffic arriving for B out to Switch B
        parser_transit = dp_transit.ofproto_parser
        for h in target_hosts_b:
            match = parser_transit.OFPMatch(eth_dst=h["mac"])
            actions = [parser_transit.OFPActionOutput(port_transit_to_b)]
            self.add_flow(
                datapath=dp_transit,
                priority=PRIORITY_REROUTE,
                match=match,
                actions=actions,
                hard_timeout=TIMEOUT_REROUTE_HARD_SEC,
            )

        # 3. On Switch B: route return traffic destined to Hosts on A towards the Transit switch
        parser_b = dp_b.ofproto_parser
        for h in target_hosts_a:
            match = parser_b.OFPMatch(eth_dst=h["mac"])
            actions = [parser_b.OFPActionOutput(port_b_to_transit)]
            self.add_flow(
                datapath=dp_b,
                priority=PRIORITY_REROUTE,
                match=match,
                actions=actions,
                hard_timeout=TIMEOUT_REROUTE_HARD_SEC,
            )

        # 4. On Transit Switch: forward return traffic arriving for A out to Switch A
        for h in target_hosts_a:
            match = parser_transit.OFPMatch(eth_dst=h["mac"])
            actions = [parser_transit.OFPActionOutput(port_transit_to_a)]
            self.add_flow(
                datapath=dp_transit,
                priority=PRIORITY_REROUTE,
                match=match,
                actions=actions,
                hard_timeout=TIMEOUT_REROUTE_HARD_SEC,
            )

        reroute_id = (dpid_a, dpid_b, transit_dpid)
        if reroute_id not in self.active_reroutes:
            self.active_reroutes.add(reroute_id)
            self.log_event(
                f"DYNAMIC ACTION [REROUTE]: Established detour path between s{dpid_a} <-> s{dpid_b} "
                f"via transit switch s{transit_dpid} (priority={PRIORITY_REROUTE}, timeout={TIMEOUT_REROUTE_HARD_SEC}s).",
                level="ACTION",
            )
