# Distributed Network Monitoring with SDN

[![OpenFlow 1.3](https://img.shields.io/badge/OpenFlow-1.3-blue.svg)](https://opennetworking.org/)
[![Mininet](https://img.shields.io/badge/Mininet-2.3.0+-orange.svg)](http://mininet.org/)
[![Ryu SDN Framework](https://img.shields.io/badge/Ryu-4.34+-green.svg)](https://ryu-sdn.org/)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8+-brightgreen.svg)](https://www.python.org/)

A distributed network-monitoring and autonomous mitigation system implemented with **Software-Defined Networking (SDN)** and **UDP Socket Programming**. End-hosts transmit periodic real-time application and system telemetry over UDP sockets to a centralized **Ryu SDN controller**. The controller continuously correlates host-level metrics with OpenFlow 1.3 flow and port counters to detect anomalies (volumetric floods, link delays, packet loss, agent crashes, and link failures) and dynamically mitigates them through reactive OpenFlow flow rules.

---

## Team Members & Responsibilities

| Person | Name | Role | Primary Responsibilities |
|---|---|---|---|
| **Person A** | **Sai** | Data Plane & Testing Lead | Mininet triangle topology, host telemetry agent loop (`agent.py`), fault injection CLI (`faults.py`), automated test runner (`test_runner.py`) |
| **Person B** | **Chinmayi** | Controller & SDN Lead | Ryu SDN controller application, multi-threaded UDP telemetry collector, OpenFlow 1.3 stats poller, anomaly correlation engine, dynamic flow response engine |
| **Person C** | **Navaneeth** | Architecture & Evaluation Lead | System architecture design, technical documentation & README, benchmark evaluation, presentation slides, and demo orchestration |

---

## System Architecture

The architecture decouples telemetry generation, centralized cross-layer correlation, and OpenFlow switching into three distinct operational planes:

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
   | Cross-Layer Correlation & Anomaly Detection Engine (Evaluates every 2.0s)                                       |
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
   |  - Periodic Statistics Poller (OFPPortStatsRequest, OFPFlowStatsRequest dispatched every 2.0s)                   |
   |  - Reactive L2 MAC Learning & Flow Installation Engine (OFPPacketIn -> OFPFlowMod Priority 10)                  |
   |  - Switch Topology Manager (s1 <-> s2 <-> s3 tracking, link status handling via OFPPortStatus)                  |
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

### Triangle Mesh Data Plane Topology
The data plane emulates a 3-switch triangle loop with redundant paths:
- **`s1`**: connected to hosts `h1` (`10.0.0.1`), `h2` (`10.0.0.2`), and the Root NAT Node (`10.0.0.254`).
- **`s2`**: connected to hosts `h3` (`10.0.0.3`), `h4` (`10.0.0.4`).
- **`s3`**: connected to hosts `h5` (`10.0.0.5`), `h6` (`10.0.0.6`).
- **Inter-Switch Trunks (`s1-s2`, `s2-s3`, `s1-s3`)**: rate-limited to **10 Mbps** using Mininet `TCLink` queueing disciplines to emulate realistic bottleneck conditions during traffic surges.

---

## Prerequisites

- **Operating System:** Ubuntu 20.04 / 22.04 LTS, Debian 11+, or the Mininet Pre-packaged VM.
- **Python:** Python 3.8 or higher.
- **Mininet:** Mininet 2.3.0+ (`openvswitch-switch` kernel datapath).
- **SDN Controller Framework:** Ryu Framework (`ryu-manager`, supporting OpenFlow 1.3).
- **Traffic Tools:** `iperf` (volumetric flood generation) and `iproute2` (`tc netem` for packet loss and latency injection).
- **Python Libraries:** `psutil` (host hardware statistics collection).

---

## Installation Steps

### 1. Update Packages & Install System Dependencies
```bash
sudo apt update
sudo apt install -y git python3 python3-pip mininet openvswitch-switch iperf iproute2 net-tools
```

### 2. Clone the Repository
```bash
git clone https://github.com/<your-username>/CN-Project.git
cd CN-Project
```

### 3. Install Python Dependencies
```bash
pip3 install --upgrade pip
pip3 install ryu psutil
```
> [!NOTE]
> If installing Ryu on newer versions of Python (3.10+), install `setuptools<58.0.0` or install inside a virtual environment to ensure proper compatibility with Ryu's eventlet dependencies.

### 4. Verify Mininet & Ryu Installation
```bash
# Verify Mininet can spin up OVS switches and run a sanity ping test
sudo mn --test pingall

# Clean up any leftover Mininet state
sudo mn -c

# Verify Ryu is installed and accessible in your PATH
ryu-manager --version
```

---

## Repository Structure

```
CN-Project/
├── README.md               # Complete project documentation and operation manual
├── project-plan.md         # Milestone timeline, team roles, and rubric mapping
├── topology/
│   └── topo.py             # Mininet 3-switch triangle topology + NAT telemetry gateway
├── agents/
│   └── agent.py            # Host telemetry agent (UDP echo reflector + periodic metrics sender)
├── faults/
│   ├── faults.py           # Mininet CLI extension commands for fault injection & logging
│   └── test_runner.py      # Automated benchmark runner for baseline and dynamic modes
├── controller/
│   └── sdn_controller.py   # Ryu SDN controller (UDP collector, poller, detector, responses)
├── docs/
│   ├── architecture.md     # Detailed architecture specifications and module interfaces
│   ├── report.md           # Formal project technical report
│   └── slides.md           # Presentation slides structure and viva defense script
└── logs/
    ├── controller.log      # Ryu controller stdout/stderr and anomaly detection log
    ├── fault_events.log    # Timestamped record of fault injections and clears
    ├── results_summary.json# Aggregated metrics table across all test runs
    └── run_*.json          # Detailed per-scenario execution telemetry captures
```

---

## How to Run Step by Step

Running the distributed monitoring system requires three concurrent terminal sessions (or running the controller as a background daemon).

### Step 1: Start the Ryu SDN Controller
Open **Terminal 1** and launch the Ryu controller. Select either **`dynamic`** or **`baseline`** mode:

```bash
cd ~/CN-Project

# Run in Dynamic Mode (anomaly detection + autonomous mitigation):
ryu-manager controller/sdn_controller.py --mode=dynamic

# OR run in Baseline Mode (learning switch + logging only):
# ryu-manager controller/sdn_controller.py --mode=baseline
```
The controller initializes:
- OpenFlow 1.3 listener on `127.0.0.1:6653`.
- UDP telemetry collector thread listening on `0.0.0.0:9999`.
- Periodic stats poller running every $2.0\text{ seconds}$.

### Step 2: Start the Mininet Triangle Topology
Open **Terminal 2** and run the topology script with root privileges:

```bash
cd ~/CN-Project
sudo python3 topology/topo.py
```
Mininet starts the three switches (`s1`, `s2`, `s3`), assigns IP/MAC addresses to `h1`-`h6`, configures the 10 Mbps bottleneck links, attaches the root NAT node (`10.0.0.254`), and launches the custom `FaultCLI`:

```text
*** Starting CLI:
mininet>
```

### Step 3: Launch Host Telemetry Agents
At the `mininet>` prompt, start the background telemetry agents on all hosts:

```bash
mininet> agents start
```
This spawns `agents/agent.py` on `h1` through `h6`. Each host:
1. Starts its UDP echo server on port `9998`.
2. Probes its designated peer host across the network.
3. Transmits JSON telemetry datagrams to `10.0.0.254:9999` once every second.

### Step 4: Inject Network & Host Faults
Test how the controller detects and mitigates abnormal conditions by issuing custom CLI commands:

```bash
# 1. UDP Flood: Generate 50 Mbps UDP flood between h1 and h4 for 20 seconds
mininet> fault flood h1 h4 20

# 2. Added Latency: Inject 200ms delay on host h2's interface
mininet> fault delay h2 200

# 3. Packet Loss: Introduce 30% packet loss on host h3's interface
mininet> fault loss h3 30

# 4. Agent Crash: Terminate the telemetry agent process on host h5
mininet> fault killagent h5

# 5. Link Failure: Bring down the direct trunk link between switches s1 and s2
mininet> fault linkdown s1 s2

# Clear any active fault and restore normal link conditions:
mininet> fault clear
```

### Step 5: Monitor Live System Logs
Open **Terminal 3** to inspect real-time detection events and controller actions:

```bash
cd ~/CN-Project

# Monitor anomaly detection and OpenFlow rule modifications:
tail -f logs/controller.log

# Monitor fault injection timestamps:
tail -f logs/fault_events.log
```

### Step 6: Shutdown & Cleanup
To cleanly exit the environment:
```bash
mininet> agents stop
mininet> exit
sudo mn -c
```

---

## Telemetry Message Format

Hosts stream JSON-serialized UDP datagrams to `10.0.0.254:9999` at $1.0\text{-second}$ intervals:

```json
{
  "host_id": "h1",
  "seq": 42,
  "timestamp": 1700000000.123,
  "metrics": {
    "app_latency_ms": 12.4,
    "req_per_sec": 85.0,
    "error_count": 0,
    "cpu_percent": 23.1,
    "mem_percent": 41.7
  }
}
```

### Telemetry Field Definitions

| Field | Type | Description |
|---|---|---|
| `host_id` | String | Unique identifier of the transmitting host (`h1` through `h6`). |
| `seq` | Integer | Monotonically increasing sequence number. Used by the controller to calculate packet loss and out-of-order delivery. |
| `timestamp` | Float | Unix epoch timestamp ($1\text{ ms}$ precision) when the telemetry sample was collected. |
| `metrics.app_latency_ms` | Float | Average round-trip time (RTT) measured over 5 UDP echo probes sent to the host's assigned peer. |
| `metrics.req_per_sec` | Float | Effective successful probe transactions completed per second. |
| `metrics.error_count` | Integer | Count of probe timeouts ($> 200\text{ ms}$) or socket errors encountered during the interval. |
| `metrics.cpu_percent` | Float | Current system-wide CPU utilization percentage sampled via `psutil`. |
| `metrics.mem_percent` | Float | Current physical memory (RAM) utilization percentage sampled via `psutil`. |

---

## Cross-Layer Detection & Response Rules

The correlation engine evaluates switch flow statistics and host telemetry against a four-quadrant decision matrix:

| Quadrant | Application Telemetry | OpenFlow Switch Stats | Likely Condition | Controller Dynamic Response | Baseline Mode Behavior |
|:---:|---|---|---|---|---|
| **I** | Latency $> 100\text{ ms}$ or Error Count $\ge 3$ | Port Bitrate $> 8\text{ Mbps}$ (Link Saturated) | **Congestion or Volumetric Flood** | Install `OFPFlowMod` Priority 100 **DROP** rule on the offending source IP/port; install Priority 50 **REROUTE** rule for victim traffic via alternate switch. | Log alert only; victim traffic suffers high latency and dropped packets. |
| **II** | Latency $> 100\text{ ms}$ or Error Count $\ge 3$ | Port Bitrate $< 3\text{ Mbps}$ (Link Underutilized) | **Host or Application Bottleneck** | Flag host as degraded; avoid unnecessary rerouting; log diagnostic alert to network operators. | Log alert only; standard switching continues. |
| **III** | Telemetry Missing ($> 5\text{ s}$ silence) | Flow Counters Incrementing ($> 0\text{ bps}$) | **Agent Crash or UDP Loss** | Raise critical agent crash alert; keep existing forwarding flow rules intact to prevent service interruption. | Log alert only; forwarding remains unaffected. |
| **IV** | Telemetry Missing ($> 5\text{ s}$ silence) | Flow Counters Inactive ($0\text{ bps}$) | **Host or Physical Link Down** | Evict stale flow entries via `OFPFC_DELETE`; recompute shortest path through triangle mesh (`s1-s3-s2`) and push failover rules. | Log alert only; packets forward into dead link (black hole). |

---

## Fault Injection Commands Reference

Custom Mininet CLI commands provided in `faults/faults.py`:

| Command | Arguments | Under-the-Hood Mechanism | Event Logged |
|---|---|---|---|
| `agents start` | None | Spawns `agent.py` in background on hosts `h1`–`h6` targeting their assigned peers (`h1 <-> h4`, `h2 <-> h5`, `h3 <-> h6`). | `agents started` |
| `agents stop` | None | Dispatches `pkill -f agent.py` across all host network namespaces. | `agents stopped` |
| `fault flood` | `<src> <dst> [secs]` | Starts background `iperf -s -u` on `<dst>` and launches `iperf -c <dst> -u -b 50M -t <secs>` from `<src>`. Saturates the 10 Mbps bottleneck trunk. | `flood <src>-><dst> <secs>s` |
| `fault delay` | `<host> <ms>` | Replaces the root egress qdisc on `<host>-eth0` with Linux `tc netem delay <ms>ms`. | `delay <host> <ms>ms` |
| `fault loss` | `<host> <percent>` | Replaces the root egress qdisc on `<host>-eth0` with Linux `tc netem loss <percent>%`. | `loss <host> <percent>%` |
| `fault killagent` | `<host>` | Sends `pkill -f -- '--id=<host>'` to terminate the telemetry agent process on that specific host. | `killagent <host>` |
| `fault linkdown` | `<sw1> <sw2>` | Issues Mininet `configLinkStatus(<sw1>, <sw2>, "down")` to simulate physical wire disconnection. | `linkdown <sw1>-<sw2>` |
| `fault clear` | None | Deletes all `tc` queueing disciplines, terminates active `iperf` processes, and brings all downed switch links back `up`. | `clear` |

---

## Running Automated Tests

The automated test runner (`faults/test_runner.py`) provides end-to-end benchmarking across all five fault scenarios in both **baseline** and **dynamic** modes.

### Usage
```bash
# Run all fault scenarios in both baseline and dynamic modes:
sudo python3 faults/test_runner.py --mode both --outdir logs/

# Run only dynamic mode with a 30-second detection window:
sudo python3 faults/test_runner.py --mode dynamic --detection-window 30

# Run a specific scenario (e.g., flood and delay):
sudo python3 faults/test_runner.py --mode dynamic --scenarios flood,delay

# Dry-run mode (simulates test execution for CI/verification without Mininet/root):
python3 faults/test_runner.py --dry-run
```

### Command-Line Arguments

| Flag | Default | Description |
|---|---|---|
| `--mode` | `both` | Controller mode to benchmark: `baseline`, `dynamic`, or `both`. |
| `--outdir` | `logs/` | Output directory where JSON metrics and summaries will be written. |
| `--detection-window` | `30` | Time window (in seconds) to observe telemetry and controller reaction. |
| `--scenarios` | `all` | Comma-separated scenario identifiers (`flood`, `delay`, `loss`, `killagent`, `linkdown`) or `all`. |
| `--topo` | `topology/topo.py` | Path to the Mininet topology script. |
| `--fault-log` | `logs/fault_events.log` | Path to timestamped fault injection log. |
| `--controller-log` | `logs/controller.log` | Path to controller log file. |
| `--dry-run` | `False` | Run simulated test harness without requiring root or a Mininet VM. |

### Automated Execution Workflow
For every scenario and mode, the test runner:
1. Spawns Mininet and the triangle topology in an isolated subprocess.
2. Initializes the root NAT gateway and launches host telemetry agents.
3. Injects the specified fault command into the interactive Mininet CLI.
4. Waits for the configured detection window while continuously monitoring `controller.log`.
5. Computes high-resolution **time-to-detect** ($\Delta t = t_{\text{detect}} - t_{\text{fault}}$) and **time-to-recover**.
6. Clears the fault condition, terminates host agents, and tears down Mininet cleanly (`mn -c`).
7. Serializes all metrics into `logs/run_<scenario>_<mode>_<timestamp>.json`.
8. Generates an aggregated summary report in `logs/results_summary.json` and renders an aligned results table to stdout.

### Sample Test Runner Output Table
```text
========================================================================================================================================
                                    AUTOMATED SDN TEST RESULTS SUMMARY
========================================================================================================================================
+----------------+----------+---------------------------+----------------------------------+----------------+-----------------+--------+
| Fault Injected | Mode     | Expected Detection        | Actual Detection                 | Time to Detect | Time to Recover | Status |
+----------------+----------+---------------------------+----------------------------------+----------------+-----------------+--------+
| UDP flood      | baseline | Congestion or flood       | Congestion or flood detected ... | 1.82s          | N/A (baseline)  | PASS   |
| Added delay    | baseline | Latency anomaly           | Latency anomaly detected on t... | 1.82s          | N/A (baseline)  | PASS   |
| Packet loss    | baseline | Loss or missing telemetry | Loss or missing telemetry det... | 1.82s          | N/A (baseline)  | PASS   |
| Agent killed   | baseline | Agent crash               | Agent crash detected on target   | 1.82s          | N/A (baseline)  | PASS   |
| Link down      | baseline | Host or link down         | Host or link down detected on... | 1.82s          | N/A (baseline)  | PASS   |
| UDP flood      | dynamic  | Congestion or flood       | Congestion or flood detected ... | 1.45s          | 2.30s           | PASS   |
| Added delay    | dynamic  | Latency anomaly           | Latency anomaly detected on t... | 1.45s          | 2.30s           | PASS   |
| Packet loss    | dynamic  | Loss or missing telemetry | Loss or missing telemetry det... | 1.45s          | 2.30s           | PASS   |
| Agent killed   | dynamic  | Agent crash               | Agent crash detected on target   | 1.45s          | 2.30s           | PASS   |
| Link down      | dynamic  | Host or link down         | Host or link down detected on... | 1.45s          | 2.30s           | PASS   |
+----------------+----------+---------------------------+----------------------------------+----------------+-----------------+--------+
Total Scenarios Run: 10 | Detected: 10/10
```

---

## Documentation Links

- **System Architecture Specification:** [docs/architecture.md](file:///c:/Users/ssmed/Downloads/CN-Project/docs/architecture.md)
- **Project Plan & Viva Rubric:** [project-plan.md](file:///c:/Users/ssmed/Downloads/CN-Project/project-plan.md)
- **Technical Report:** [docs/report.md](file:///c:/Users/ssmed/Downloads/CN-Project/docs/report.md)
- **Presentation Slides:** [docs/slides.md](file:///c:/Users/ssmed/Downloads/CN-Project/docs/slides.md)