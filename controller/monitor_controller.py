import json
import os
import socket
from collections import deque

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event


class MonitorController(app_manager.RyuApp):
    """Telemetry monitoring controller with loop-safe BFS forwarding."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    FORWARD_PRIORITY = 10
    BLOCK_PRIORITY = 100
    FORWARD_IDLE_TIMEOUT = 60
    FORWARD_COOKIE = 0xBEEF

    def __init__(self, *args, **kwargs):
        super(MonitorController, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.host_locations = {}  # MAC -> (dpid, port)
        self.last_seq = {}
        self.blocked_hosts = set()
        self.network_links = {}  # (src_dpid, src_port, dst_dpid, dst_port)
        self.running = True
        self.mode = os.getenv("MONITOR_MODE", "dynamic").strip().lower()
        if self.mode not in ("baseline", "dynamic"):
            raise ValueError("MONITOR_MODE must be baseline or dynamic")
        self.logger.info("Controller mode: %s", self.mode)
        self.collector_thread = hub.spawn(self.udp_collector)
        self.stats_thread = hub.spawn(self.poll_statistics)
        self.logger.info("Monitoring controller started")

    # -------------------------- BFS topology --------------------------
    def get_switch_adjacency(self):
        graph = {}
        for src, src_port, dst, dst_port in list(self.network_links):
            graph.setdefault(src, []).append((dst, src_port, dst_port))
            graph.setdefault(dst, []).append((src, dst_port, src_port))
        return graph

    def find_path(self, start, destination):
        """Return [(switch, output_port, next_switch_input_port), ...]."""
        if start == destination:
            return []
        graph = self.get_switch_adjacency()
        queue = deque([start])
        parent = {start: None}
        parent_port = {}
        while queue:
            current = queue.popleft()
            if current == destination:
                route = []
                node = destination
                while parent[node] is not None:
                    previous = parent[node]
                    out_port, in_port = parent_port[node]
                    route.append((previous, out_port, in_port))
                    node = previous
                return list(reversed(route))
            for neighbour, out_port, in_port in graph.get(current, []):
                if neighbour not in parent:
                    parent[neighbour] = current
                    parent_port[neighbour] = (out_port, in_port)
                    queue.append(neighbour)
        return None

    def get_inter_switch_ports(self):
        ports = set()
        for src, src_port, dst, dst_port in self.network_links:
            ports.add((src, src_port))
            ports.add((dst, dst_port))
        return ports

    def get_access_ports(self, datapath):
        linked = self.get_inter_switch_ports()
        return [p for p in datapath.ports
                if p < datapath.ofproto.OFPP_MAX
                and (datapath.id, p) not in linked]

    def flood_tree(self, source_dpid, source_in_port, data):
        """Flood via a BFS spanning tree, avoiding loops in cyclic topologies."""
        graph = self.get_switch_adjacency()
        queue = deque([source_dpid])
        parent = {source_dpid: None}
        while queue:
            current = queue.popleft()
            for neighbour, out_port, _ in graph.get(current, []):
                if neighbour not in parent:
                    parent[neighbour] = current
                    queue.append(neighbour)
        for dpid, parent_dpid in parent.items():
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue
            outputs = set(self.get_access_ports(dp))
            if parent_dpid is not None:
                # Find the edge back to the BFS parent and do not send back on it.
                for neighbour, out_port, _ in graph.get(dpid, []):
                    if neighbour == parent_dpid:
                        outputs.discard(out_port)
            # Add only tree-child links.
            for neighbour, out_port, _ in graph.get(dpid, []):
                if parent.get(neighbour) == dpid:
                    outputs.add(out_port)
            if dpid == source_dpid:
                outputs.discard(source_in_port)
            for port in sorted(outputs):
                out = dp.ofproto_parser.OFPPacketOut(
                    datapath=dp, buffer_id=dp.ofproto.OFP_NO_BUFFER,
                    in_port=dp.ofproto.OFPP_CONTROLLER,
                    actions=[dp.ofproto_parser.OFPActionOutput(port)], data=data)
                dp.send_msg(out)

    def install_flow(self, dp, in_port, src_mac, dst_mac, out_port):
        parser = dp.ofproto_parser
        match = parser.OFPMatch(in_port=in_port, eth_src=src_mac, eth_dst=dst_mac)
        actions = [parser.OFPActionOutput(out_port)]
        instructions = [parser.OFPInstructionActions(
            dp.ofproto.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, cookie=self.FORWARD_COOKIE,
            priority=self.FORWARD_PRIORITY,
            idle_timeout=self.FORWARD_IDLE_TIMEOUT,
            match=match, instructions=instructions))

    def install_route(self, route, src_dpid, src_in_port, destination, src_mac, dst_mac):
        if destination is None:
            return False
        dst_dpid, dst_port = destination
        if src_dpid == dst_dpid:
            dp = self.datapaths.get(src_dpid)
            if dp is None:
                return False
            self.install_flow(dp, src_in_port, src_mac, dst_mac, dst_port)
            return True
        if not route:
            return False
        # Source switch
        first_dpid, first_out, _ = route[0]
        dp = self.datapaths.get(first_dpid)
        if dp is None:
            return False
        self.install_flow(dp, src_in_port, src_mac, dst_mac, first_out)
        # Intermediate switches: the input port on switch i is the
        # destination-side port recorded by the preceding route hop.
        for index in range(1, len(route)):
            dpid, out_port, _next_switch_in_port = route[index]
            in_port = route[index - 1][2]
            dp = self.datapaths.get(dpid)
            if dp is None:
                return False
            self.install_flow(dp, in_port, src_mac, dst_mac, out_port)
        # Destination switch
        last_dpid, _, dst_in_port = route[-1]
        dp = self.datapaths.get(dst_dpid)
        if dp is None:
            return False
        self.install_flow(dp, dst_in_port, src_mac, dst_mac, dst_port)
        return True

    def clear_forwarding_flows(self):
        """Delete only this controller's forwarding flows after topology changes."""
        for dp in list(self.datapaths.values()):
            parser = dp.ofproto_parser
            ofp = dp.ofproto
            delete = parser.OFPFlowMod(
                datapath=dp,
                cookie=self.FORWARD_COOKIE,
                cookie_mask=0xFFFFFFFFFFFFFFFF,
                command=ofp.OFPFC_DELETE,
                table_id=ofp.OFPTT_ALL,
                out_port=ofp.OFPP_ANY,
                out_group=ofp.OFPG_ANY,
                match=parser.OFPMatch(),
            )
            dp.send_msg(delete)

    # -------------------------- OpenFlow -------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        parser = dp.ofproto_parser
        actions = [parser.OFPActionOutput(dp.ofproto.OFPP_CONTROLLER,
                                          dp.ofproto.OFPCML_NO_BUFFER)]
        instructions = [parser.OFPInstructionActions(dp.ofproto.OFPIT_APPLY_ACTIONS,
                                                     actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=0,
                                     match=parser.OFPMatch(),
                                     instructions=instructions))
        self.logger.info("Table-miss rule installed on switch %s", dp.id)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth_list = pkt.get_protocols(ethernet.ethernet)
        if not eth_list:
            return
        eth = eth_list[0]
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        src, dst, src_dpid = eth.src, eth.dst, dp.id
        if (src_dpid, in_port) not in self.get_inter_switch_ports():
            self.host_locations[src] = (src_dpid, in_port)
        destination = self.host_locations.get(dst)
        is_multicast = bool(int(dst.split(":")[0], 16) & 1)
        if destination is None or is_multicast:
            self.flood_tree(src_dpid, in_port, msg.data)
            return
        dst_dpid, dst_port = destination
        route = [] if src_dpid == dst_dpid else self.find_path(src_dpid, dst_dpid)
        if route is None:
            self.logger.warning("No BFS route from switch=%s to switch=%s",
                                src_dpid, dst_dpid)
            return
        if not self.install_route(route, src_dpid, in_port, destination, src, dst):
            self.logger.warning("Could not install complete route from %s to %s",
                                src_dpid, dst_dpid)
            return
        first_out = dst_port if src_dpid == dst_dpid else route[0][1]
        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=msg.buffer_id, in_port=in_port,
            actions=[parser.OFPActionOutput(first_out)], data=data))

    # -------------------------- Telemetry ------------------------------
    def udp_collector(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("0.0.0.0", 9999))
            sock.settimeout(1.0)
            self.logger.info("Telemetry collector listening on UDP port 9999")
            while self.running:
                try:
                    data, address = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                try:
                    message = json.loads(data.decode("utf-8"))
                    required = {"host_id", "seq", "timestamp", "metrics"}
                    if not isinstance(message, dict) or not required.issubset(message):
                        self.logger.warning("Invalid telemetry structure from %s", address)
                        continue
                    host_id, seq = message["host_id"], message["seq"]
                    if not isinstance(host_id, str) or not host_id:
                        self.logger.warning("Invalid host_id from %s", address)
                        continue
                    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                        self.logger.warning("Invalid sequence number from %s", address)
                        continue
                    metrics = message["metrics"]
                    if not isinstance(metrics, dict):
                        self.logger.warning("Invalid metrics from %s", address)
                        continue
                    timestamp = message["timestamp"]
                    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
                        self.logger.warning("Invalid timestamp from %s", address)
                        continue
                    previous = self.last_seq.get(host_id)
                    if previous is not None and seq > previous + 1:
                        self.logger.warning("TELEMETRY GAP host=%s expected=%s received=%s missing=%s",
                                            host_id, previous + 1, seq, seq - previous - 1)
                    elif previous is not None and seq <= previous:
                        self.logger.warning("DUPLICATE/OUT-OF-ORDER host=%s seq=%s previous=%s",
                                            host_id, seq, previous)
                    if previous is None or seq > previous:
                        self.last_seq[host_id] = seq
                    self.logger.info("TELEMETRY host=%s seq=%s metrics=%s",
                                     host_id, seq, json.dumps(metrics))
                    thresholds = {"app_latency_ms": 200, "req_per_sec": 4500,
                                  "error_count": 10, "cpu_percent": 85,
                                  "mem_percent": 90}
                    for metric, threshold in thresholds.items():
                        value = metrics.get(metric)
                        if (isinstance(value, (int, float)) and
                                not isinstance(value, bool) and value > threshold):
                            self.logger.warning("ANOMALY host=%s metric=%s value=%s threshold=%s",
                                                host_id, metric, value, threshold)
                            if self.mode == "dynamic":
                                self.block_host(host_id)
                            else:
                                self.logger.info("BASELINE: detection only; no action taken for host=%s",
                                                 host_id)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.logger.warning("Malformed JSON from %s", address)
        except OSError:
            self.logger.exception("UDP collector failed")
        finally:
            sock.close()

    def block_host(self, host_id):
        host_ips = {"h1": "10.0.0.1", "h2": "10.0.0.2",
                    "h3": "10.0.0.3", "h4": "10.0.0.4",
                    "h5": "10.0.0.5", "h6": "10.0.0.6"}
        host_switches = {"h1": 1, "h2": 1, "h3": 2,
                         "h4": 2, "h5": 3, "h6": 3}
        if host_id not in host_ips:
            self.logger.warning("Cannot block unknown host: %s", host_id)
            return
        if host_id in self.blocked_hosts:
            return
        dp = self.datapaths.get(host_switches[host_id])
        if dp is None:
            self.logger.warning("Switch unavailable for host %s", host_id)
            return
        parser = dp.ofproto_parser
        match = parser.OFPMatch(eth_type=0x0800, ipv4_src=host_ips[host_id])
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, priority=self.BLOCK_PRIORITY, match=match,
            instructions=[], hard_timeout=30))
        self.blocked_hosts.add(host_id)
        self.logger.warning("DYNAMIC_ACTION: blocked host=%s ip=%s for 30 seconds",
                            host_id, host_ips[host_id])
        hub.spawn(self.clear_blocked_host, host_id)

    def clear_blocked_host(self, host_id):
        hub.sleep(30)
        self.blocked_hosts.discard(host_id)
        self.logger.info("Temporary block expired for host=%s", host_id)

    # -------------------------- Topology events ------------------------
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def switch_state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            self.logger.info("Switch connected: %s", dp.id)
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)
            self.logger.info("Switch disconnected: %s", dp.id)

    @set_ev_cls(event.EventLinkAdd)
    def link_add_handler(self, ev):
        link = ev.link
        src, dst = link.src, link.dst
        key = (src.dpid, src.port_no, dst.dpid, dst.port_no)
        reverse = (dst.dpid, dst.port_no, src.dpid, src.port_no)
        self.network_links[key] = True
        self.network_links[reverse] = True
        self.clear_forwarding_flows()
        self.logger.info("LINK ADDED: switch=%s port=%s <-> switch=%s port=%s",
                         src.dpid, src.port_no, dst.dpid, dst.port_no)

    @set_ev_cls(event.EventLinkDelete)
    def link_delete_handler(self, ev):
        link = ev.link
        src, dst = link.src, link.dst
        key = (src.dpid, src.port_no, dst.dpid, dst.port_no)
        reverse = (dst.dpid, dst.port_no, src.dpid, src.port_no)
        self.network_links.pop(key, None)
        self.network_links.pop(reverse, None)
        self.clear_forwarding_flows()
        self.logger.warning("LINK DOWN: switch=%s port=%s <-> switch=%s port=%s",
                            src.dpid, src.port_no, dst.dpid, dst.port_no)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        msg = ev.msg
        dpid, port_no = msg.datapath.id, msg.desc.port_no
        if (msg.reason == ofproto_v1_3.OFPPR_DELETE or
                (msg.reason == ofproto_v1_3.OFPPR_MODIFY and
                 msg.desc.state & ofproto_v1_3.OFPPS_LINK_DOWN)):
            for key in list(self.network_links):
                if ((key[0] == dpid and key[1] == port_no) or
                        (key[2] == dpid and key[3] == port_no)):
                    self.network_links.pop(key, None)
            self.clear_forwarding_flows()
            self.logger.warning("PORT DOWN: switch=%s port=%s", dpid, port_no)

    # -------------------------- Statistics -----------------------------
    def poll_statistics(self):
        while self.running:
            for dp in list(self.datapaths.values()):
                parser = dp.ofproto_parser
                try:
                    dp.send_msg(parser.OFPFlowStatsRequest(dp))
                    dp.send_msg(parser.OFPPortStatsRequest(
                        dp, 0, dp.ofproto.OFPP_ANY))
                except Exception:
                    self.logger.exception("Failed to request stats from switch %s", dp.id)
            hub.sleep(10)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply(self, ev):
        for stat in ev.msg.body:
            self.logger.info("FLOW_STATS switch=%s priority=%s packets=%s bytes=%s",
                             ev.msg.datapath.id, stat.priority,
                             stat.packet_count, stat.byte_count)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply(self, ev):
        for stat in ev.msg.body:
            self.logger.info("PORT_STATS switch=%s port=%s rx_packets=%s tx_packets=%s rx_bytes=%s tx_bytes=%s",
                             ev.msg.datapath.id, stat.port_no, stat.rx_packets,
                             stat.tx_packets, stat.rx_bytes, stat.tx_bytes)

    def close(self):
        self.running = False
        super(MonitorController, self).close()
