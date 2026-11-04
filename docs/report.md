# Distributed Network Monitoring with SDN: Cross-Layer Telemetry and Dynamic Anomaly Mitigation

**Course:** Computer Networks — SDN & Socket Programming  
**Team Members:**  
- **Sai (Person A):** Mininet Topology, Host Telemetry Agents, Fault Injection, Test Automation  
- **Chinmayi (Person B):** Ryu SDN Controller, Multi-Threaded UDP Collector, Stats Poller, Anomaly Correlator, Dynamic Response Engine  
- **Navaneeth (Person C):** Documentation, Architecture Specification, Test Logging & Analysis, Presentation Slides  

---

## 1. Problem Statement and Architecture

### 1.1 Motivation and Problem Statement
Modern enterprise and data center networks face complex performance degradations, ranging from volumetric denial-of-service (DoS) attacks and physical link failures to transient congestion, packet corruption, and host-level software degradation. Traditional network management systems typically operate in silos:
1. **Network-Plane Blindness:** Standard Software-Defined Networking (SDN) controllers, operating via OpenFlow, have centralized visibility into flow counters, port statistics, and topology link states. However, the SDN control plane is completely blind to end-host application-level health, CPU/memory starvation, socket buffer drops, and fine-grained perceived application round-trip time (RTT).
2. **Host-Plane Impotence:** Host-level monitoring agents and APM (Application Performance Monitoring) tools collect high-fidelity process and socket metrics, but lack network control authority. An application detecting severe packet loss or latency cannot independently reconfigure transit switches, divert around congested inter-switch trunks, or throttle upstream malicious ingress.

To solve this bifurcation, this project develops a **Distributed Network-Monitoring System with SDN**. End-hosts stream real-time application and system telemetry over UDP sockets to a centralized Ryu SDN controller. The controller simultaneously polls OpenFlow 1.3 flow and port counters from Open vSwitch (OVS) switches. By continuously correlating application-layer health against physical-layer and flow-layer traffic patterns, the controller detects anomalies with high precision, distinguishes between host faults and network congestion, and autonomously enacts dynamic flow rule mitigations (rate-limiting, flow dropping, and dynamic rerouting).

### 1.2 System Architecture Overview
The architecture is structured into three interconnected functional planes:

```
+-----------------------------------------------------------------------------------+
|                                 APPLICATION PLANE                                 |
|   +-----------------------+     +-----------------------+     +---------------+   |
|   |  Host h1 Agent        |     |  Host h2 Agent        | ... | Host h6 Agent |   |
|   |  - UDP Echo (:9998)   |     |  - UDP Echo (:9998)   |     |  - UDP Echo   |   |
|   |  - Active RTT Probing |     |  - Active RTT Probing |     |  - Probing    |   |
|   |  - psutil Sys Metrics |     |  - psutil Sys Metrics |     |  - Metrics    |   |
|   +-----------+-----------+     +-----------+-----------+     +-------+-------+   |
+---------------|-----------------------------|-------------------------|-----------+
                | UDP Datagrams (JSON)        | UDP Datagrams (JSON)    |
                +----------------------+      |                         |
                                       v      v                         v
+-----------------------------------------------------------------------------------+
|                      CONTROL PLANE (Ryu SDN Controller)                           |
|  +-----------------------------------------------------------------------------+  |
|  | Multi-Threaded UDP Telemetry Collector (Listening on Port 9999 via NAT)    |  |
|  | - JSON Schema Validation & Ingestion    - Sequence Gap / Loss Detector      |  |
|  | - Sliding Window Telemetry Store        - Host Liveness Watchdog            |  |
|  +------------------------------------+----------------------------------------+  |
|                                       |                                           |
|  +------------------------------------+----------------------------------------+  |
|  | OpenFlow 1.3 Statistics Poller & L2 Learning Switch                         |  |
|  | - OFPPortStatsRequest (Periodic)        - OFPFlowStatsRequest (Periodic)    |  |
|  | - Port Delta Bytes / Delta Packets      - Per-Flow Bitrate Computation      |  |
|  +------------------------------------+----------------------------------------+  |
|                                       |                                           |
|  +------------------------------------v----------------------------------------+  |
|  | Cross-Layer Correlation & Anomaly Detection Engine                          |  |
|  | - Multi-Dimensional Heuristic Matrix    - Anomaly Classifier                |  |
|  | - Host vs. Network Fault Isolation      - Execution State (Baseline/Dynamic)|  |
|  +------------------------------------+----------------------------------------+  |
|                                       |                                           |
|  +------------------------------------v----------------------------------------+  |
|  | Dynamic Response & Flow Rule Engine                                         |  |
|  | - OFPFlowMod (Drop / Rate-Limit)       - OFPFlowMod (Dynamic Path Reroute) |  |
|  | - Table Flush & Stale Rule Eviction    - Incident Auditing & Event Logger  |  |
|  +------------------------------------+----------------------------------------+  |
+---------------------------------------|-------------------------------------------+
                                        | OpenFlow 1.3 Control Channel (TCP :6653)
                                        v
+-----------------------------------------------------------------------------------+
|                         DATA PLANE (Mininet Triangle Mesh)                        |
|                                                                                   |
|                 [h1]      [h2]                           [h3]      [h4]           |
|                   \        /                               \        /             |
|                  +----------+      10 Mbps Link           +----------+            |
|                  |    s1    |=============================|    s2    |            |
|                  +----------+                             +----------+            |
|                       \\                                       //                 |
|                        \\             10 Mbps Link            //                  |
|                         \\                                   //                   |
|                          \\                                 //                    |
|                           +-----------------+---------------+                     |
|                           |                 |                                     |
|                           |            +----------+                               |
|                           |            |    s3    |                               |
|                           |            +----------+                               |
|                     NAT Gateway          /      \                                 |
|                    10.0.0.254:9999     [h5]    [h6]                               |
+-----------------------------------------------------------------------------------+
```

---

## 2. Socket Implementation

The host-side telemetry subsystem is designed around low-overhead, asynchronous socket communication implemented in Python 3. Because telemetry is sampled at high frequency (1-second intervals), TCP connection establishment and head-of-line blocking would introduce unwanted latency and memory overhead. Therefore, UDP was chosen as the transport protocol.

### 2.1 Host Telemetry Agent Architecture (`agent.py`)
Each emulated host (`h1` through `h6`) executes an autonomous telemetry agent consisting of two concurrent threads:
1. **UDP Echo Server Daemon:** Binds to `0.0.0.0:9998` on a background daemon thread. It operates a tight loop receiving datagrams and immediately reflecting them back to the sender (`s.sendto(data, addr)`). This provides an active probing target for other hosts.
2. **Telemetry Generator & Active Probe Loop:** Runs on the main thread at a configurable sampling frequency (default $1.0\text{ s}$). During each interval, the agent:
   - Dispatches $N=5$ active UDP echo probe datagrams with sequence tokens (`p0` to `p4`) to its assigned peer across the network with a strict deadline timeout ($200\text{ ms}$).
   - Records individual probe RTTs, calculating average RTT (`app_latency_ms`), completed transactions per second (`req_per_sec`), and timeout error count (`error_count`).
   - Gathers host OS utilization metrics using `psutil`, capturing `cpu_percent` and `mem_percent`.
   - Constructs a standardized JSON payload and transmits it via a non-blocking UDP socket to the controller's telemetry endpoint (`10.0.0.254:9999`).

### 2.2 Telemetry Datagram Specification
Every datagram sent by an agent complies with the agreed JSON schema:
```json
{
  "host_id": "h1",
  "seq": 142,
  "timestamp": 1700000142.385,
  "metrics": {
    "app_latency_ms": 14.25,
    "req_per_sec": 5.0,
    "error_count": 0,
    "cpu_percent": 12.4,
    "mem_percent": 38.2
  }
}
```
Key fields include:
- `host_id`: Unique identifier for topological mapping.
- `seq`: Monotonically increasing sequence integer. The collector logs gaps and out-of-order datagrams; gaps do not currently trigger packet-loss classification.
- `timestamp`: Epoch timestamp recorded immediately before transmission to detect transmission delay and drift.
- `metrics.app_latency_ms`: Real RTT experienced by end-to-end application traffic.
- `metrics.error_count`: Number of timed-out active probe packets during the interval.

### 2.3 Controller UDP Telemetry Collector
Within the Ryu controller application, an asynchronous daemon thread initializes an `AF_INET`, `SOCK_DGRAM` socket bound to `0.0.0.0:9999`. 
- **Buffer Ingestion & Parsing:** Uses `recvfrom(4096)` wrapped in exception-handling blocks to safely decode UTF-8 byte streams and parse JSON data. Malformed packets are discarded without interrupting the thread.
- **Sequence Continuity & Liveness Tracking:** For each known `host_id`, the collector maintains an internal state structure tracking:
  $$\Delta \text{seq} = \text{seq}_{\text{current}} - \text{seq}_{\text{previous}}$$
  If $\Delta \text{seq} > 1$, missing packets are flagged as telemetry packet loss.
- **Sliding-Window Cache:** A ring buffer stores the most recent 10 telemetry samples per host, supplying smoothed running averages to the anomaly detection engine.

---

## 3. Mininet + SDN Setup

### 3.1 Network Topology and Bandwidth Bottlenecks
The network topology is instantiated using Mininet 2.3.0 with Open vSwitch (`OVSSwitch`) configured for the OpenFlow 1.3 protocol. As depicted in Section 1.2:
- **Core Triangle:** Three switches (`s1`, `s2`, `s3`) form a cyclical triangle mesh.
- **Link Constraints:** All inter-switch links (`s1-s2`, `s2-s3`, `s1-s3`) are shaped using Linux Traffic Control (`TCLink`) with an engineered bandwidth capacity of **10 Mbps** and a default delay of $2\text{ ms}$. This deliberate bottleneck ensures that high-volume test traffic triggers real buffer queuing, packet dropping, and latency inflation.
- **Host Attachment:** Six hosts are distributed uniformly: `h1, h2` attached to `s1`; `h3, h4` attached to `s2`; `h5, h6` attached to `s3`.
- **Active Probe Pairs:** Each host is configured to probe a peer on a non-local switch (`h1` probes `h4`, `h2` probes `h5`, `h3` probes `h6`, etc.), forcing probe packets across the bottleneck inter-switch links.

### 3.2 Out-of-Band Controller Access via NAT
Mininet isolates host network namespaces. To allow host agents inside Mininet namespaces to communicate with the Ryu controller listening on the host OS, a Mininet NAT node (`net.addNAT()`) is attached to switch `s1` with IP `10.0.0.254/8`. The NAT node sets up `iptables` IP forwarding and masquerading, routing UDP packets addressed to `10.0.0.254:9999` directly to the host loopback/controller interface, preserving complete separation between the simulated data plane and the telemetry control channel.

### 3.3 Ryu Controller and OpenFlow 1.3 Configuration
The Ryu SDN controller runs as an independent process connecting to `127.0.0.1:6653`. During the OpenFlow switch handshake:
- **Table-Miss Flow Entry:** Ryu installs a default flow rule (Priority 0) matching all wildcard packets, directing them to the controller via `OFPActionOutput(OFPP_CONTROLLER)`.
- **Reactive L2 MAC Learning:** Upon receiving `EventOFPPacketIn`, the controller records the source MAC address and ingress port. If the destination MAC is known, a unicast `OFPFlowMod` entry is written with an idle timeout of 30 seconds; otherwise, the packet is flooded.

---

## 4. Socket-SDN Integration (Cross-Layer Correlation)

### 4.1 Cross-Layer Correlation Concept
The defining innovation of this system is the **Correlator Engine**, which fuses application-layer telemetry (collected via UDP sockets) with network-layer flow and port statistics (collected via OpenFlow 1.3).

```
   [ Host Telemetry Stream ]                     [ Switch OpenFlow Stats ]
        (Port 9999 UDP)                            (Port 6653 TCP/OF1.3)
               |                                             |
               v                                             v
  +-------------------------+                   +-------------------------+
  | Host Telemetry Store    |                   | Switch Flow/Port Store  |
  | - app_latency_ms        |                   | - tx_bytes / rx_bytes   |
  | - error_count / loss    |                   | - delta_bytes_sec (bps) |
  | - last_seen timestamp   |                   | - active flow matches   |
  +------------+------------+                   +------------+------------+
               \                                             /
                \                                           /
                 v                                         v
          +-------------------------------------------------------+
          |         Cross-Layer Anomaly Correlator Engine         |
          |  Evaluates multi-dimensional heuristics every 5.0s    |
          +-------------------------------------------------------+
```

### 4.2 OpenFlow Statistics Poller
A background `hub.spawn` thread in Ryu executes a periodic polling loop every $2.0\text{ seconds}$:
1. Emits `OFPPortStatsRequest` and `OFPFlowStatsRequest` to all registered datapath switches (`s1, s2, s3`).
2. Listens for `EventOFPPortStatsReply` and `EventOFPFlowStatsReply`.
3. Computes instantaneous throughput per port and per flow:
   $$\text{Throughput (bps)} = \frac{(\text{bytes}_{t} - \text{bytes}_{t - \Delta t}) \times 8}{\Delta t}$$

### 4.3 Implemented Detection and Response Rules

The following rules describe the current controller implementation. Sequence gaps are logged by the telemetry collector, but are not by themselves a classification rule. The controller does not currently handle OpenFlow port-status events.

| Condition | Controller classification | Baseline behavior | Dynamic behavior |
|---|---|---|---|
| App latency >30 ms and host access-port throughput >8 Mbps | Congestion/flood | Log anomaly | Find highest-rate host MAC, install priority 200 drop rules with 30 s hard timeout, then attempt alternate-path rules for other flows at priority 150 with 60 s timeout |
| Probe loss >=20% | Packet loss | Log anomaly | Detection only; no reroute is issued |
| App latency >30 ms and host access-port throughput <=8 Mbps | Host/app problem | Log anomaly | Log only; no network reroute |
| No telemetry record yet, or last telemetry >5 s ago, and flow/port counters show activity | Agent crash | Log anomaly | Alert only; keep forwarding state |
| No telemetry record yet, or last telemetry >5 s ago, and no active counters | Host/link down | Log anomaly | Delete host-MAC-matching flows and attempt alternate-path rules using the triangle mapping |

For a host that has never reported, the implementation treats telemetry as missing immediately instead of waiting five seconds. It can therefore emit host/link-down anomalies before agents start. Evaluation runs should establish agent telemetry before fault injection and should not count these startup events as fault detections.

### 4.4 Dynamic Response Mechanism

Dynamic responses are implemented with fixed rules for the configured three-switch triangle. Congestion handling drops the selected source MAC across connected switches and attempts detours for non-offending flows. Host/link-down handling deletes flows matching the host MAC and attempts detours. Packet loss, high latency at lower throughput, and agent crashes are logged without a network reroute. These are source-level behaviors; their successful operation and service impact remain unverified until live evaluation.

---

## 5. Testing Methodology

### 5.1 Test Framework and Fault Injection
The test plan uses a dedicated fault-injection module (`faults.py`) integrated into the Mininet CLI. The following five conditions are planned for live evaluation:

1. **UDP Flood Congestion:** The runner starts a 50 Mbps UDP `iperf` flow from `h1` to `h4` for 20 seconds.
2. **Link Latency:** Injected using Linux Traffic Control NetEm on `h2`:
   ```bash
   tc qdisc replace dev h2-eth0 root netem delay 200ms
   ```
3. **Packet Loss:** Injected via NetEm on `h3`:
   ```bash
   tc qdisc replace dev h3-eth0 root netem loss 30%
   ```
4. **Agent Process Stop:** The runner terminates the telemetry agent process on `h5`:
   ```bash
   pkill -f -- '--id=h5'
   ```
5. **Host Access Link Down:** The runner disables the `h1-s1` access link through Mininet's `configLinkStatus`:
   ```bash
   fault linkdown h1 s1
   ```

### 5.2 Comparative Execution: Baseline vs. Dynamic
The evaluation plan compares each of the five fault scenarios under two operational modes, in separate approved live batches:
- **Baseline Mode:** The controller learns MAC forwarding rules and logs anomalies to `logs/controller.log`; it does not issue anomaly mitigation.
- **Dynamic Mode:** The full closed-loop control system is enabled. Anomaly detection triggers automated flow table modifications, path recalculation, and traffic suppression.

### 5.3 Quantitative Performance Metrics
The intended measurements for each live run are:
- **Time to Detect (TTD):** Elapsed duration between fault injection timestamp and the controller logging the verified anomaly.
- **Time to Recover (TTR):** Elapsed duration from verified fault injection until two consecutive ICMP probes satisfy the run-relative RTT/loss threshold. Probes are sampled during the fault and after clear, so recovery can be detected during mitigation or after restoration. This confirms reachability for the probe pair, not telemetry-agent recovery.
- **RTT and probe loss:** One ICMP echo per nominal one-second sample; each loss sample is 0% or 100% and is therefore coarse.
- **Throughput:** RX+TX host-interface byte-counter rate in Mbps. This is aggregate host traffic, not flow-specific application goodput.

---

## 6. Performance Evaluation

### 6.1 Results status

**Results are from the final evaluation run (one run per scenario and mode; 5/5 faults detected in both modes).** Detection time runs from the verified fault-injection timestamp to the first matching controller anomaly on the faulted host. Because each configuration was run once, small differences between modes are within run-to-run variation.

*Changes made after the first evaluation (which detected 2/5 in both modes):* the congestion latency threshold was lowered from 100 ms to 30 ms (measured flood RTT was 60-84 ms); evaluation agents use a 0.6 s probe timeout so a 200 ms netem delay is not counted as loss; the link-down scenario now disables the h1-s1 access link, because cutting a switch-switch link in a triangle leaves every host reachable; the controller counts a host as active only if traffic is sent by it or seen on its own access port; and the runner counts a detection only if the alert is on the faulted host.

*Limitations:* recovery time, RTT, throughput and probe loss are not reported. The runner's recovery metric measures time to two healthy probes after cleanup, so it mostly reflects fault duration, and its independent ICMP probe showed 0% loss during the 30% packet-loss fault (cause not determined). No benefit of dynamic mitigation on recovery was measured.

| Scenario | Mode | Detection time |
|---|---|---|
| UDP flood | Baseline | 9.25 s |
| UDP flood | Dynamic | 6.65 s |
| Added delay | Baseline | 6.05 s |
| Added delay | Dynamic | 1.88 s |
| Packet loss | Baseline | 3.83 s |
| Packet loss | Dynamic | 4.45 s |
| Agent stopped | Baseline | 6.62 s |
| Agent stopped | Dynamic | 2.30 s |
| Link down | Baseline | 8.64 s |
| Link down | Dynamic | 9.35 s |

Detection time uses the unique verified fault-injection event timestamp and the first matching controller anomaly timestamp. The success event is appended only after injection verification, while its timestamp records when the fault effect was applied. Action latency is measured from that anomaly to the first action attributable to the same anomaly category and affected host; ambiguous or unrelated actions yield null.

The runner records one ICMP echo per nominal one-second interval from the scenario's source host to its peer. RTT is reported in milliseconds. Probe packet loss is recorded per echo as 0% or 100%, so it is a coarse sample rather than a bulk-flow loss estimate. Throughput is independently calculated in Mbps from RX+TX byte-counter deltas on the probed host interface; it includes all host traffic and is not flow-specific goodput. Counter intervals use actual sample timestamps. Recovery time is the elapsed time from verified injection until two consecutive probe samples, during the fault or after clearing it, have zero loss and RTT no greater than `max(2 × baseline median RTT, baseline median RTT + 20 ms)`. Recovery remains null if valid baseline probes or independent confirmation are missing. The post-clear observation window is 30 seconds. Results must retain these method limits.

---

## 7. Conclusion

This project implements an integrated network monitoring architecture combining host-level UDP socket telemetry with OpenFlow 1.3 SDN control. Evaluation results are reported in Section 6.1. The design goals are:
1. **Cross-Layer Observability is Essential:** Network statistics alone cannot differentiate between legitimate bursty traffic and malicious congestion, nor can they reveal end-host resource exhaustion. Correlating application-layer RTT and error metrics with flow byte counters resolves operational ambiguity.
2. **Autonomic Remediation:** Dynamic mode is intended to issue mitigation actions after detected anomalies. Both modes detected all five faults in the final run; no recovery benefit of mitigation was measured.
3. **Resilience Against False Positives:** Dynamic agent-crash handling is designed to preserve forwarding state when telemetry stops but traffic counters remain active. This behavior has not yet been confirmed in a live evaluation.

Future extensions could incorporate rolling exponential weighted moving averages (EWMA) with dynamic z-scores instead of static thresholds, and offload socket packet parsing directly to programmable data planes using P4 or eBPF.

---

## 8. References

1. **Ryu SDN Framework Documentation:** Ryu SDN Framework Community, *Ryu SDN Framework: OpenFlow Controller Documentation*, Release 4.34, 2021. Available: `https://ryu.readthedocs.io/`
2. **Mininet Network Emulator:** B. Lantz, B. Heller, and N. McKeown, "A network in a laptop: rapid prototyping for software-defined networks," in *Proceedings of the 9th ACM SIGCOMM Workshop on Hot Topics in Networks (HotNets-IX)*, 2010.
3. **OpenFlow Switch Specification:** Open Networking Foundation (ONF), *OpenFlow Switch Specification Version 1.3.5 (Protocol version 0x04)*, ONF TS-023, Mar. 2015.
4. **Python psutil Library:** G. Rodola, *psutil: Cross-platform lib for process and system monitoring in Python*, Version 5.9.0, 2022. Available: `https://psutil.readthedocs.io/`
5. **Software-Defined Networking Principles:** N. Feamster, J. Rexford, and E. Zegura, "The road to SDN: an intellectual history of programmable networks," *ACM SIGCOMM Computer Communication Review*, vol. 44, no. 2, pp. 87-98, 2014.
