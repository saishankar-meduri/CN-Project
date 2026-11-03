# System Architecture: Distributed Network Monitoring with SDN

This document specifies the end-to-end architectural design, module interfaces, data flows, and state machines for the Distributed Network Monitoring and Dynamic Response System.

---

## 1. System Block Diagram (ASCII Art)

```
========================================================================================================================
                                                  APPLICATION PLANE
========================================================================================================================
   +------------------------------------+   +------------------------------------+   +-------------------------------+
   | Host h1 (10.0.0.1)                 |   | Host h2 (10.0.0.2)                 |   | Hosts h3..h6                  |
   |                                    |   |                                    |   |                               |
   |  +------------------------------+  |   |  +------------------------------+  |   |  +-------------------------+  |
   |  | UDP Echo Server (:9998)      |  |   |  | UDP Echo Server (:9998)      |  |   |  | UDP Echo Server (:9998) |  |
   |  | - Reflector for active RTT   |  |   |  | - Reflector for active RTT   |  |   |  | - Reflector for active  |  |
   |  +------------------------------+  |   |  +------------------------------+  |   |  +-------------------------+  |
   |                 ^                  |   |                 ^                  |   |                 ^             |
   |  +--------------+---------------+  |   |  +--------------+---------------+  |   |  +--------------+----------+  |
   |  | Active Probe & Telemetry     |  |   |  | Active Probe & Telemetry     |  |   |  | Active Probe & Telemetry|  |
   |  | Agent Loop (agent.py)        |  |   |  | Agent Loop (agent.py)        |  |   |  | Agent Loop (agent.py)   |  |
   |  | - Probes peer (e.g., h4)     |  |   |  | - Probes peer (e.g., h5)     |  |   |  | - Probes peers          |  |
   |  | - Samples psutil CPU/mem     |  |   |  | - Samples psutil CPU/mem     |  |   |  | - Samples psutil        |  |
   |  | - Serializes JSON datagrams  |  |   |  | - Serializes JSON datagrams  |  |   |  | - Serializes JSON       |  |
   |  +--------------+---------------+  |   |  +--------------+---------------+  |   |  +--------------+----------+  |
   +-----------------|------------------+   +-----------------|------------------+   +-----------------|-------------+
                     |                                        |                                        |
                     | UDP JSON Telemetry                     | UDP JSON Telemetry                     | UDP JSON
                     | (Destination: 10.0.0.254:9999)         | (Destination: 10.0.0.254:9999)         | Telemetry
                     |                                        |                                        |
                     +----------------------------------------+----------------------------------------+
                                                              |
                                                              v
========================================================================================================================
                                                   CONTROL PLANE
                                     Ryu SDN Controller (127.0.0.1:6653)
========================================================================================================================
   +-----------------------------------------------------------------------------------------------------------------+
   | UDP Telemetry Collector Module (Port 9999 Daemon Thread)                                                        |
   |  - Non-blocking UDP socket binding 0.0.0.0:9999                                                                  |
   |  - JSON schema parsing, timestamp synchronization, and malformed payload filtration                            |
   |  - Host sequence tracker: monitors monotonic seq IDs; detects gaps (dropped packets)                           |
   |  - Host telemetry sliding window (10-sample ring buffer per host): stores RTT, req/s, errors, CPU, mem          |
   +---------------------------------------------------+-------------------------------------------------------------+
                                                       |
                                                       | Updates Host State Table
                                                       v
   +-----------------------------------------------------------------------------------------------------------------+
   | In-Memory Host & Telemetry State Cache                                                                          |
   |   { host_id: { last_seen, seq, app_latency_ms, req_per_sec, error_count, cpu_percent, mem_percent } }             |
   +---------------------------------------------------+-------------------------------------------------------------+
                                                       ^
                                                       | Correlates application stats with switch stats
                                                       v
   +-----------------------------------------------------------------------------------------------------------------+
   | Cross-Layer Correlation & Anomaly Detection Engine (Evaluates every 5.0s)                                       |
   |   1. Computes delta byte rates (bps) from OpenFlow Port & Flow tables                                           |
   |   2. Retrieves latest host telemetry metrics (latency, error count, sequence continuity)                       |
   |   3. Compares metrics against multi-dimensional threshold matrix:                                               |
   |        - High Latency + Saturated Link (> 8 Mbps)       --> Congestion / Volumetric Flood (Quadrant I)          |
   |        - High Latency + Low Bandwidth (< 3 Mbps)        --> Host / Application Bottleneck (Quadrant II)         |
   |        - Missing Telemetry + Flow Counters Incrementing --> Agent Crash / Monitoring Dropout (Quadrant III)     |
   |        - Missing Telemetry + Flow Counters Inactive     --> Physical Link Down / Host Dead (Quadrant IV)        |
   +---------------------------------------------------+-------------------------------------------------------------+
                                                       |
                                                       | Dispatches Anomaly Event & Mitigation Directive
                                                       v
   +-----------------------------------------------------------------------------------------------------------------+
   | Response Engine (Dual Mode: Baseline vs. Dynamic)                                                               |
   |                                                                                                                 |
   |   [ Baseline Mode ]                                [ Dynamic Mode ]                                             |
   |   - Write alert to logs/fault_events.log           - Write alert with high-resolution timestamps                |
   |   - Maintain standard L2 learning switch behavior  - Select mitigation strategy:                                |
   |   - Zero flow table modifications                  * Quadrant I: Install OFPFlowMod DROP / Meter rule           |
   |                                                    * Quadrant I & IV: Recalculate shortest path, install reroute|
   |                                                    * Quadrant III: Maintain forwarding rules, alert admin       |
   +---------------------------------------------------+-------------------------------------------------------------+
                                                       |
                                                       | Issues OpenFlow 1.3 FlowMod / Barrier / Request Messages
                                                       v
   +-----------------------------------------------------------------------------------------------------------------+
   | OpenFlow 1.3 Protocol & Switch Management Module                                                                |
   |  - Periodic Statistics Poller (OFPPortStatsRequest, OFPFlowStatsRequest dispatched every 5.0s)                   |
   |  - Reactive L2 MAC Learning & Flow Installation Engine (OFPPacketIn -> OFPFlowMod Priority 1)                   |
   |  - Fixed triangle switch map; no OFPPortStatus handler in current controller                                 |
   +---------------------------------------------------+-------------------------------------------------------------+
                                                       |
                                                       | OpenFlow 1.3 TCP Control Channel (Port 6653)
                                                       v
========================================================================================================================
                                                    DATA PLANE
                                    Open vSwitch (OVS) Emulation in Mininet
========================================================================================================================
             +-----------------------+                         +-----------------------+
             | Switch s1 (OVS)       |                         | Switch s2 (OVS)       |
             | - Port 1: h1-eth0     |=== 10 Mbps TCLink ======| - Port 1: h3-eth0     |
             | - Port 2: h2-eth0     |    (Inter-Switch Trunk) | - Port 2: h4-eth0     |
             | - Port 3: s1-eth3(s2) |                         | - Port 3: s2-eth3(s1) |
             | - Port 4: s1-eth4(s3) |                         | - Port 4: s2-eth4(s3) |
             | - Port 5: NAT (root)  |                         +-----------+-----------+
             +-----------+-----------+                                     |
                         \\                                               //
                          \\                                             //
                   10 Mbps \\                                           // 10 Mbps
                    TCLink  \\                                         //   TCLink
                             \\                                       //
                              +-------------------+-------------------+
                                                  |
                                      +-----------+-----------+
                                      | Switch s3 (OVS)       |
                                      | - Port 1: h5-eth0     |
                                      | - Port 2: h6-eth0     |
                                      | - Port 3: s3-eth3(s1) |
                                      | - Port 4: s3-eth4(s2) |
                                      +-----------------------+
========================================================================================================================
```

---

## 2. Component Descriptions

### 2.1 Host Telemetry Agent (`agents/agent.py`)
- **Echo Reflector Server:** Runs a UDP server on port `9998`. Reflects back any UDP probe packet received from peer hosts. This provides the ground-truth application-level RTT without needing a heavyweight HTTP or TCP daemon.
- **Active Prober:** Every probe interval (default $1.0\text{ s}$), sends a series of 5 small datagrams (`p0` through `p4`) to the target host's echo server. Measures the round-trip duration with millisecond precision, counting timeouts ($200\text{ ms}$ threshold) as errors.
- **Resource Monitor:** Uses Python's `psutil` library to read instant CPU load percentage and physical memory utilization.
- **Telemetry Transmitter:** Encapsulates the metrics into a JSON object with a strictly incrementing sequence number `seq` and epoch timestamp, sending the datagram to `10.0.0.254:9999`.

### 2.2 Mininet Topology & Network Infrastructure (`topology/topo.py`)
- **Triangle Mesh:** Switches `s1`, `s2`, and `s3` are arranged in a 3-switch cycle. The dual-path property allows testing redundant paths and dynamic rerouting (`s1-s2` direct path vs. `s1-s3-s2` alternate path).
- **Traffic Control Link (`TCLink`):** Configures Linux kernel queueing disciplines (`qdisc`) on inter-switch links to enforce a hard maximum bandwidth of **10 Mbps** and a base delay of $2\text{ ms}$.
- **NAT Node Integration:** A virtual NAT router (`10.0.0.254/8`) is attached to switch `s1`. It maps host namespace egress addressed to `10.0.0.254:9999` to the root system loopback interface, allowing host agents to deliver UDP packets to the Ryu controller without requiring an external physical network interface.

### 2.3 Ryu Controller Subsystems (`controller/`)
- **UDP Collector Subsystem:** Binds to UDP port `9999` in a daemon thread. Decodes inbound JSON payloads, updates host liveness records (`last_seen`), and logs sequence gaps and out-of-order arrivals. Sequence gaps do not currently trigger the packet-loss classifier.
- **OpenFlow Statistics Poller:** Periodically emits `OFPPortStatsRequest` and `OFPFlowStatsRequest` messages to connected datapaths on a five-second cycle. Receives reply events, calculates delta byte counts, and computes per-port and per-flow transfer rates in bits per second (bps).
- **Cross-Layer Correlator & Detector:** Combines host telemetry with switch port/flow rates to detect congestion/flood, probe loss, host/app latency, missing-agent telemetry, and host/link inactivity. See the current implementation table in `docs/report.md`.
- **Response & Mitigation Engine:**
  - *Baseline Mode:* Acts as a standard OpenFlow learning switch; detection alerts are logged to file, but no corrective flow entries are installed.
  - *Dynamic Mode:* Installs reactive, high-priority flow modifications (`OFPFlowMod`):
    - **Drop Rule (Priority 200):** Drops traffic matching the selected source MAC for 30 seconds.
    - **Reroute Rule (Priority 150):** Attempts alternate triangle paths for non-offending or host/link-down traffic, with a 60-second hard timeout.
    - **Flow Eviction:** Deletes flows matching a host MAC after host/link-down classification.

---

## 3. Data Flow Descriptions

### 3.1 Telemetry Data Flow Path
```
[Host Agent (h1)]
       |
       | 1. Active Probing: Send UDP datagrams to peer h4 (:9998); measure RTT and loss
       | 2. Metric Collection: Read CPU/Mem via psutil
       | 3. Construct JSON: { host_id: "h1", seq: N, timestamp: T, metrics: {...} }
       | 4. Socket Transmit: UDP sendto("10.0.0.254", 9999)
       v
[Mininet NAT Node (10.0.0.254)]
       |
       | 5. Kernel iptables NAT forwarding to host localhost
       v
[Ryu Controller: UDP Collector Thread (:9999)]
       |
       | 6. recvfrom(4096)
       | 7. JSON decode & validate
       | 8. Log sequence gaps and out-of-order arrivals
       | 9. Commit to in-memory Host State Cache
```

### 3.2 OpenFlow Statistics Path
```
[Ryu Poller Greenlet (Hub)]
       |
       | 1. Wakeup timer fires every 5.0s
       | 2. Emit OFPPortStatsRequest and OFPFlowStatsRequest to switches s1, s2, s3
       v
[Open vSwitch Data Plane (s1, s2, s3)]
       |
       | 3. Read internal OVS hardware/kernel flow counters (rx_bytes, tx_bytes, packet_count)
       | 4. Pack into OFPPortStatsReply and OFPFlowStatsReply OpenFlow 1.3 messages
       v
[Ryu Controller: OpenFlow Event Handlers]
       |
       | 5. EventOFPPortStatsReply / EventOFPFlowStatsReply received
       | 6. Compute throughput: delta_bytes * 8 / delta_time = current_bitrate_bps
       | 7. Commit to in-memory Switch Flow/Port State Cache
```

### 3.3 Anomaly Detection & Dynamic Response Path
```
[Cross-Layer Correlator Engine]
       |
       | 1. Pull latest telemetry metrics from Host State Cache (RTT, errors, seq continuity)
       | 2. Pull latest switch port/flow bitrates from Switch State Cache
       | 3. Correlate host IP/MAC with switch ports and active flow rules
       | 4. Apply the implemented telemetry and bitrate rules (see docs/report.md):
       |
       +---> Case A: Latency > 100ms AND host access-port bitrate > 8 Mbps (Congestion / Flood)
       |            |
       |            +--> Baseline Mode: Log [CONGESTION_ALERT] to disk
       |            +--> Dynamic Mode:  Drop selected source MAC (priority 200);
       |                                attempt detours for other traffic (priority 150)
       |
       +---> Case B: Latency > 100ms AND host access-port bitrate <= 8 Mbps (Host / App Problem)
       |            |
       |            +--> Baseline & Dynamic: Log anomaly; no network reroute
       |
       +---> Case C: No telemetry yet / silence > 5s AND Flow Counters > 0 (Agent Crash)
       |            |
       |            +--> Baseline & Dynamic: Log anomaly; dynamic mode preserves active flow rules
       |
       +---> Case D: No telemetry yet / silence > 5s AND Flow Counters == 0 (Link / Node Down)
                    |
                    +--> Baseline Mode: Log anomaly
                    +--> Dynamic Mode:  Delete host-MAC flows and attempt triangle detours
```

---

## 4. Sequence Diagram: Baseline vs. Dynamic Execution (ASCII)

The following is a conceptual message flow, not an observed experiment. Numeric performance values and recovery claims are intentionally omitted. The current implementation details and limits are summarized in the report's detection table.

```
Host Agent (h1)         Switch (s1)             UDP Collector          Correlator Engine       Response Engine
      |                      |                        |                        |                       |
      |--- UDP Telemetry --->|                        |                        |                       |
      |   (JSON to :9999)    |--- Forward via NAT --->|                        |                       |
      |                      |                        |--- Store Host Stat --->|                       |
      |                      |                        |                        |                       |
      |                      |<-- OFPPortStatsReq ----| (every 5.0s)           |                       |
      |                      |--- OFPPortStatsReply ->|                        |                       |
      |                      |                        |--- Store Flow Stat --->|                       |
      |                      |                        |                        |                       |
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                                  FAULT INJECTION: 50 Mbps UDP FLOOD OCCURS
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
      |                      |                        |                        |                       |
      |-- Telemetry: high RTT / probe loss ---------->|                        |                       |
      |                      |                        |--- Update Latency ---->|                       |
      |                      |<-- OFPPortStatsReq ----|                        |                       |
      |                      |-- OFPPortStatsReply -->|                        |                       |
      |                      |                        |--- Update Bitrate ---->|                       |
      |                      |                        |                        |                       |
      |                      |                        |                        |-- Correlate: ---------+
      |                      |                        |                        |   RTT high + BW high  |
      |                      |                        |                        |   => CONGESTION       |
      |                      |                        |                        |-----------------------+
      |                      |                        |                        |                       |
      |                      |                        |                        |-- Anomaly Event ----->|
      |                      |                        |                        |                       |
+==============================================================================================================+
| BRANCH 1: BASELINE MODE                                                                                      |
+==============================================================================================================+
      |                      |                        |                        |                       |
      |                      |                        |                        |                       |-- Log Warning:
      |                      |                        |                        |                       |   "Congestion on
      |                      |                        |                        |                       |    s1-s2 port 3"
      |                      |                        |                        |                       |   (No FlowMod sent)
      |                      |                        |                        |                       |
      | (No mitigation flow modifications are issued in baseline mode)                                         |
      |                      |                        |                        |                       |
+==============================================================================================================+
| BRANCH 2: DYNAMIC MODE                                                                                       |
+==============================================================================================================+
      |                      |                        |                        |                       |
      |                      |                        |                        |                       |-- Classify Action:
      |                      |                        |                        |                       |   1. Drop flood
      |                      |                        |                        |                       |   2. Reroute victim
      |                      |                        |                        |                       |
      |                      |<===================== OFPFlowMod (Priority 200) ========================|
      |                      |                       [MATCH: eth_src=selected host MAC]                |
      |                      |                       [ACTION: DROP]                                    |
      |                      |                                                                         |
      |                      |<===================== OFPFlowMod (Priority 150) ========================|
      |                      |                       [MATCH: destination host MAC]                     |
      |                      |                       [ACTION: OUTPUT port 4 (s1->s3)]                  |
      |                      |                                                                         |
      |                      |-- Flood dropped; buffer empties; victim traffic traverses s1->s3->s2 ---|
      |                      |                        |                        |                       |
      |-- Later telemetry sample -------------------->|                        |                       |
      |                      |                        |--- Update Host Stat -->|                       |
      |                      |                        |                        |-- No recovery metric is   |
      |                      |                        |                        |   emitted by current code |
```

---

## 5. Security, Robustness, and State Integrity

1. **Graceful Degraded Handling:** The UDP collector uses structured `try...except` parsing blocks to catch `json.JSONDecodeError` and `KeyError` exceptions, isolating corrupted payloads and preventing malicious or incomplete datagrams from crashing the controller.
2. **Watchdog Timers:** To prevent persistent stale state during silent drops, every host entry in the telemetry store includes an epoch expiration watchdog timer ($5.0\text{ s}$). If the watchdog expires, the node transitions to an `UNRESPONSIVE` state, triggering Quadrant III or IV analysis.
3. **Configured Alternate Paths:** The reroute helper uses the fixed triangle mapping and installs destination-MAC rules at priority 150. Live operation and loop behavior remain pending evaluation.
