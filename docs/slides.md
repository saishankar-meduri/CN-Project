# Distributed Network Monitoring with SDN
## Presentation Slides & Viva Guide

---

# Slide 1: Title Slide

## Distributed Network Monitoring with SDN
### Cross-Layer Host Telemetry & Dynamic OpenFlow Control

**Course:** Computer Networks — SDN & Socket Programming  
**Institution:** Department of Computer Science & Engineering  

**Project Team:**
- **Sai (Person A):** Mininet Topology, Host Telemetry Agents, Fault Injection Engine
- **Chinmayi (Person B):** Ryu SDN Controller, UDP Collector, Flow Poller, Anomaly Correlator
- **Navaneeth (Person C):** Documentation, Architecture Specification, Test Logging, Evaluation

---

# Slide 2: Problem Statement & Motivation

### The Blind Spots of Modern Network Management

- **Network-Plane Blindness (SDN Limitations):**
  - OpenFlow switches track byte counts, packet counts, and port states.
  - Switches cannot see host-level health: CPU spikes, memory exhaustion, application round-trip time (RTT), or socket buffer drops.
  - Cannot distinguish whether traffic degradation is caused by network congestion or a slow end-host.

- **Host-Plane Impotence (Host Monitoring Limitations):**
  - APM tools (e.g., Datadog, Prometheus node-exporter) measure application latency and process health.
  - End-hosts have **no network control authority**: cannot reroute flows, throttle upstream attackers, or configure switch queues.

- **Our Solution: Cross-Layer Synergy**
  - End-hosts stream UDP telemetry (application RTT, CPU, memory, errors) to an SDN controller.
  - Ryu controller fuses host telemetry with OpenFlow 1.3 switch counters.
  - Closed-loop automated mitigation: rapid detection, root-cause isolation, and dynamic flow reconfiguration.

---

# Slide 3: System Architecture Overview

```
 [ Host h1 ]   [ Host h2 ]   ...   [ Host h6 ]   <- Application Plane
      |             |                   |
      +=============+===================+
                    |  UDP Telemetry (JSON :9999 via NAT)
                    v
    +-----------------------------------------------+
    |            Ryu SDN Controller (c0)            |   <- Control Plane
    |  - Multi-Threaded UDP Telemetry Collector     |
    |  - OpenFlow 1.3 Flow & Port Statistics Poller |
    |  - Cross-Layer Anomaly Correlator Engine      |
    |  - Dynamic Response Engine (Baseline/Dynamic) |
    +-----------------------+-----------------------+
                            |  OpenFlow 1.3 (:6653)
                            v
            +-------------------------------+
            |    Mininet Data Plane Mesh    |   <- Data Plane
            |    s1 ====== s2 ====== s3     |
            |   (10 Mbps Bottleneck Links)  |
            +-------------------------------+
```

- **Clean Layered Separation:**
  - Application plane: Autonomous telemetry agents with active probe reflection.
  - Control plane: Correlates host metrics with switch counters in real time.
  - Data plane: Open vSwitch triangle mesh with physical-layer bandwidth constraints.

---

# Slide 4: Socket Implementation: Host Telemetry Agent

### Lightweight Python Daemon (`agents/agent.py`)

- **Dual-Threaded Architecture:**
  - **Thread 1 (UDP Echo Server on Port 9998):** Reflects inbound probe packets immediately to measure ground-truth peer RTT.
  - **Thread 2 (Active Prober & Telemetry Emitter):** Dispatches $N=5$ probe packets every 1.0s to peer host (`p0`..`p4`), records timeout errors, collects `psutil` system metrics, and emits JSON telemetry.

- **JSON Telemetry Specification (Agreed Interface):**
  ```json
  {
    "host_id": "h1",
    "seq": 42,
    "timestamp": 1700000042.105,
    "metrics": {
      "app_latency_ms": 14.25,
      "req_per_sec": 5.0,
      "error_count": 0,
      "cpu_percent": 18.4,
      "mem_percent": 42.1
    }
  }
  ```

- **Transport Design Decisions:**
  - **UDP over TCP:** Avoids TCP connection handshake overhead, head-of-line blocking, and congestion backoff during network degradation.
  - **Sequence Number Tracking (`seq`):** Logs missing or out-of-order telemetry datagrams; sequence gaps do not currently classify probe packet loss.

---

# Slide 5: Mininet Topology & Setup

### Triangle Mesh with Bottleneck Links (`topology/topo.py`)

- **Switch Mesh Topology:**
  - 3 Open vSwitch instances (`s1`, `s2`, `s3`) speaking OpenFlow 1.3.
  - Dual-path property: Traffic between `s1` and `s2` can traverse the direct link or bypass via `s1 -> s3 -> s2`.

- **Bandwidth Constraints (`TCLink`):**
  - Inter-switch links: **10 Mbps** bandwidth limit with 2ms base delay.
  - Host-to-switch links: 100 Mbps unconstrained access.
  - Forces realistic queuing, bufferbloat, and packet drops under heavy load.

- **Host Distribution:**
  - `h1, h2` attached to `s1`
  - `h3, h4` attached to `s2`
  - `h5, h6` attached to `s3`
  - Cross-switch probe pairings (`h1` probes `h4`, `h2` probes `h5`, etc.).

- **Out-of-Band NAT Gateway:**
  - Root namespace bridge at `10.0.0.254:9999` attached to `s1`.
  - Routes host UDP telemetry directly to controller process without contaminating data-plane flow tables.

---

# Slide 6: SDN Controller Subsystems

### Modular Ryu Application Architecture

- **1. UDP Collector Module:**
  - Listens on `0.0.0.0:9999` using a non-blocking background daemon thread.
  - Validates JSON format, checks monotonic sequence numbers, tracks packet drops.
  - Stores a 10-sample sliding-window ring buffer per host.

- **2. OpenFlow 1.3 Statistics Poller:**
  - Periodic polling loop triggered every $2.0\text{ seconds}$.
  - Dispatches `OFPPortStatsRequest` and `OFPFlowStatsRequest`.
  - Calculates instantaneous delta bitrates:
    $$\text{Bitrate (bps)} = \frac{(\text{bytes}_t - \text{bytes}_{t - \Delta t}) \times 8}{\Delta t}$$

- **3. Topology & L2 Forwarding Engine:**
  - Reactive MAC learning via `OFPPacketIn`.
  - Default Priority 0 table-miss flow rule forwarding to controller.
  - Computes shortest paths across active switches using Dijkstra's algorithm.

---

# Slide 7: Cross-Layer Detection Algorithm

### 4-Quadrant Anomaly Classification Matrix

| Quadrant | Application Telemetry | OpenFlow Switch Counters | Inferred Root Cause | Action in Baseline | Action in Dynamic Mode |
|---|---|---|---|---|---|
| **I** | Latency High ($> 100\text{ms}$) OR Errors ($> 1$) | Port/Flow Bitrate High ($> 8\text{ Mbps}$) | **Network Congestion / UDP Flood** | Log Warning | Drop offender flow (P=100); Reroute victim flows (P=50) |
| **II** | Latency High ($> 100\text{ms}$) | Port/Flow Bitrate Normal ($< 3\text{ Mbps}$) | **Host / Application Bottleneck** | Log Warning | Flag host; preserve network paths (avoid false reroute) |
| **III** | Telemetry Missing ($> 5.0\text{s}$) | Flow Counters Incrementing ($\Delta \text{pkts} > 0$) | **Agent Crash / Socket Drop** | Log Alert | Alert operator; maintain active forwarding state |
| **IV** | Telemetry Missing ($> 5.0\text{s}$) | Flow Counters Zero / Port Down | **Host / Physical Link Failure** | Log Critical | Flush stale flow rules; install failover reroute via s3 |

- **Root Cause Isolation:** Resolves ambiguity between software slowdowns and physical link congestion.

---

# Slide 8: Dynamic Response Engine

### Closing the Control Loop with OpenFlow FlowMod

- **Operational Modes:**
  - `--mode baseline`: Learning switch behavior; anomalies logged to disk; **no corrective flow modifications**.
  - `--mode dynamic`: Closed-loop mitigation; installs reactive OpenFlow 1.3 flow modifications.

- **Mitigation Primitives:**
  - **Offender Flow Suppression (Priority 200):**
    ```python
    match = parser.OFPMatch(eth_src=offending_mac)
    actions = []  # Empty action list = HARD DROP
    inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
    ```
  - **Dynamic Traffic Rerouting (Priority 150):**
    ```python
    match = parser.OFPMatch(eth_dst=target_mac)
    actions = [parser.OFPActionOutput(port=alternate_path_port)]
    inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
    ```
  - **Stale Rule Eviction:** Deletes flows matching a host MAC after host/link-down classification.

---

# Slide 9: Fault Injection Suite (`faults/faults.py`)

### Built-in Mininet CLI Commands for Reproducible Testing

- **1. UDP Volumetric Flood:**
  `mininet> fault flood h1 h4 20`
  *Generates 50 Mbps UDP flood using `iperf` over the 10 Mbps bottleneck link for 20 seconds.*

- **2. NetEm Latency Spike:**
  `mininet> fault delay h1 200`
  *Injects 200 ms artificial delay into `h1`'s egress interface using Linux Traffic Control (`tc netem`).*

- **3. NetEm Packet Loss:**
  `mininet> fault loss h1 30`
  *Injects 30% artificial packet drop on `h1-eth0` interface.*

- **4. Agent Crash Simulation:**
  `mininet> fault killagent h1`
  *Sends SIGKILL (`pkill -f`) to `h1`'s telemetry script while active host traffic continues.*

- **5. Physical Link Down:**
  `mininet> fault linkdown h1 s1`
  *Severs the primary switch-switch trunk between `s1` and `s2`.*

- **6. Cleanup:**
  `mininet> fault clear`
  *Removes all `tc` disciplines, terminates background `iperf`, and restores links.*

---

# Slide 10: Demo Walkthrough

### Step-by-Step Viva Execution Procedure

```
Terminal 1 (SDN Controller):
  $ .venv/bin/python tools/ryu_manager.py controller/sdn_controller.py --mode=dynamic
  [INFO] Confirm collector startup and switch connections in the actual controller log

Terminal 2 (Mininet Network):
  $ sudo python3 topology/topo.py
  mininet> pingall
  mininet> agents start
  [fault] agents started across h1..h6

Step 3: Trigger UDP Flood Fault
  mininet> fault flood h1 h4 25

Step 4: Real-Time Controller Observability
  [ANOMALY] Copy actual timestamped detection event from controller.log
  [ACTION] Copy actual mitigation event, if emitted
  [INFO] Service recovery measurement: pending independent traffic instrumentation
```

---

# Slide 11: Experimental Results (Final Run)

Final run: one run per scenario and mode, 5/5 faults detected in both modes. Earlier JSON and `logs/test_report.md` values are simulated and not used.

| Fault Injected | Mode | Detection time |
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

---

# Slide 12: Measurement Method and Status

- Detection time: fault injection timestamp to the matching controller anomaly timestamp.
- Recovery time is not reported: the runner measures time to two healthy probes after cleanup, which mostly reflects fault duration.
- RTT and probe loss: one ICMP echo per nominal one-second sample (loss is coarse 0%/100% per echo).
- Throughput: RX+TX byte-counter rate on the probed host interface, aggregate rather than flow-specific goodput.
- One run per scenario and mode on the final code (5/5 detected in both modes); small differences between modes are within run-to-run variation.

---

# Slide 13: Performance Graphs (Description)

### What the Visualizations Will Show

No time-series plots are included; only detection times are reported.

---

# Slide 14: Conclusion & Key Learnings

### Contributions & Engineering Takeaways

- **Holistic Network Observability:**
  - The design combines push-based host socket telemetry with polled SDN switch counters for cross-layer monitoring.

- **Autonomous Closed-Loop Control:**
  - Dynamic detection and mitigation are implemented; no recovery benefit over baseline was measured.

- **High Reliability & Resilience:**
  - Final evaluation: 5/5 faults detected in both modes (one run each).

- **Future Work:**
  - Machine learning classification (k-means / isolation forests) for multivariate telemetry.
  - eBPF / XDP socket integration for sub-millisecond kernel-level metric harvesting.

---

# Slide 15: Q&A / Viva Discussion Points

### Anticipated Questions & Technical Justifications

- **Q1: Why use UDP instead of TCP for host telemetry?**
  *Ans:* UDP avoids connection setup and transport retransmission. The collector logs telemetry sequence gaps and out-of-order arrivals, but gaps do not currently classify packet loss.

- **Q2: Does telemetry traffic contribute to link congestion?**
  *Ans:* Each JSON payload is ~180 bytes sent at 1 Hz. Across 6 hosts, telemetry generates ~1.08 KB/s (< 0.01% of the 10 Mbps link capacity). Furthermore, NAT routes telemetry directly out-of-band to the controller machine.

- **Q3: How does the controller prevent forwarding loops during rerouting?**
  *Ans:* The controller uses priority 150 for reroute rules and priority 1 for ordinary learned forwarding rules. Its configured triangle detours are source-level behavior; loop behavior has not yet been verified in a live run.

**Thank you! Any questions?**
