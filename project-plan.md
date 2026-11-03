# Computer Networks Project: Distributed Network Monitoring with SDN

Course project: SDN-Socket Programming (Jackfruit Mini Project). Graded in two deliverables, D1 (15 marks) and D2 (25 marks).

## Problem Statement

> Develop a distributed network-monitoring system where hosts transmit telemetry through UDP sockets. The SDN controller should combine application telemetry with network-flow statistics to identify abnormal conditions.

## Team and Roles

| Person | Name | Responsibility |
|---|---|---|
| A | Sai | Mininet topology, host telemetry agents, fault injection, baseline/dynamic test scripts |
| B | Chinmayi | Ryu controller: UDP collector, flow stats polling, anomaly detection, dynamic responses |
| C | Navaneeth | Report, architecture diagram, README, test logs, results, slides and demo |

All three must be able to explain the whole system in the viva, not just their own part.

## System Overview

```
+-----------+   UDP telemetry (JSON)   +---------------------------+
|  Host h1  | -----------------------> |                           |
|  (agent)  |                          |        Ryu SDN            |
+-----------+                          |        Controller         |
+-----------+   UDP telemetry (JSON)   |                           |
|  Host h2  | -----------------------> |  1. UDP collector         |
|  (agent)  |                          |  2. Flow stats poller     |
+-----------+                          |  3. Correlator / detector |
     ...                               |  4. Response engine       |
      |                                +-------------+-------------+
      |        OpenFlow 1.3                          |
      +------------- Switches (Mininet) -------------+
        (stats replies in, flow rules out: drop, reroute, rate-limit)
```

**Data sources the controller combines**
1. **Application telemetry** from hosts over UDP (latency, request rate, errors, CPU, memory).
2. **Network-flow statistics** from switches over OpenFlow (bytes, packets, rates per flow and port).

**What the controller does with them**
- Detects abnormal conditions by correlating both sources.
- Responds dynamically by installing or changing flow rules (see the response table below).

## Tools

- Mininet (network emulation)
- Ryu controller, OpenFlow 1.3
- Python 3 (`socket`, `json`, `threading`, `psutil`)
- `iperf` and `tc netem` (fault injection)
- matplotlib or a terminal log (visualization of results)

## Topology

Use 3 switches in a triangle (s1, s2, s3) with 4 to 6 hosts attached. The redundant path is required so the controller can reroute around a failed or congested link.

## Telemetry Message Format (agreed interface between A and B)

Each host sends one UDP datagram per interval (default 1 second):

```json
{
  "host_id": "h1",
  "seq": 42,
  "timestamp": 1700000000.123,
  "metrics": {
    "app_latency_ms": 12.4,
    "req_per_sec": 85,
    "error_count": 0,
    "cpu_percent": 23.1,
    "mem_percent": 41.7
  }
}
```

Controller listens on a fixed UDP port (proposed: `9999`). Any change to this format must be agreed by A and B. The `seq` field is used to detect lost datagrams.

## Detection and Response Logic (starting rules)

| App telemetry | Flow stats | Likely condition | Controller response |
|---|---|---|---|
| Latency high | Throughput very high | Congestion or flood | Drop or rate-limit the offending source; reroute other flows |
| Latency high | Throughput normal | Host or application problem | Flag host; log only |
| Telemetry missing | Flows still active | Agent crash or UDP loss | Alert; keep flows |
| Telemetry missing | No flows | Host or link down | Remove stale flows; reroute via alternate path |

Start with fixed thresholds. If time permits, upgrade to a rolling mean with a z-score.

The controller runs in two modes, switchable by a flag:
- **Baseline:** plain learning switch, detection logging only, no responses.
- **Dynamic:** detection plus the responses above.

## Fault Injection Scenarios (for testing)

1. UDP flood between two hosts using `iperf`.
2. Added delay on a link using `tc netem`.
3. Packet loss on a link using `tc netem`.
4. Host agent killed.
5. Link brought down in Mininet.

Each scenario is run in both baseline and dynamic mode for comparison.

## Task Breakdown

### Person A: Sai
- [x] Create Mininet topology (3-switch triangle, 4 to 6 hosts)
- [x] Write the host telemetry agent script
- [x] Write scripts for each fault injection scenario
- [x] Write a test runner that executes each fault in baseline and dynamic mode and saves metrics
- [x] Share the topology and agent scripts with the team

### Person B: Chinmayi
- [x] Set up Ryu with a basic learning switch
- [x] Implement the threaded UDP telemetry collector (handle malformed JSON, detect lost `seq`)
- [x] Implement periodic flow and port stats polling
- [x] Implement correlation and anomaly detection rules
- [x] Implement dynamic responses (drop/rate-limit, reroute, flow cleanup)
- [x] Add baseline/dynamic mode flag
- [x] Log detected anomalies and actions taken with timestamps

### Person C: Navaneeth
- [x] Draw the architecture diagram
- [x] Draft the report, structured around the rubric headings: problem and architecture, socket implementation, Mininet + SDN, socket-SDN integration, testing, performance evaluation, conclusion, references
- [x] Write the README with install and run steps (the repo must be reproducible from scratch)
- [x] After A and B's code works: run each fault scenario in both modes, save logs and screenshots, fill in the results table
- [x] Build presentation slides and record a demo video

## Milestones

### D1 (15 marks): working end-to-end base system

| # | Milestone | Owner(s) | Target date |
|---|---|---|---|
| 1 | Mininet + Ryu running, basic switching works | A, B | ____ |
| 2 | One host sends telemetry, controller prints it | A, B | ____ |
| 3 | Flow stats polling working | B | ____ |
| 4 | Basic detection rules working | B | ____ |
| 5 | Architecture diagram and D1 demo ready | C (all review) | ____ |

### D2 (25 marks): dynamic behaviour, evaluation, documentation

| # | Milestone | Owner(s) | Target date |
|---|---|---|---|
| 6 | Dynamic responses working (drop, reroute) | B | ____ |
| 7 | All fault scenarios tested in baseline and dynamic mode | A, C | ____ |
| 8 | Results table and graphs complete | C | ____ |
| 9 | Report, README, slides and demo complete | C (all review) | ____ |
| 10 | Viva prep: everyone walks through the whole system | A, B, C | ____ |

## Results Table (to be filled by Person C)

| Fault injected | Expected detection | Mode | Actual detection | Time to detect | Time to recover | Throughput | Latency | Packet loss |
|---|---|---|---|---|---|---|---|---|
| UDP flood | Congestion or flood | Baseline | | | | | | |
| UDP flood | Congestion or flood | Dynamic | | | | | | |
| Added delay | Latency anomaly | Baseline | | | | | | |
| Added delay | Latency anomaly | Dynamic | | | | | | |
| Packet loss | Loss or missing telemetry | Baseline | | | | | | |
| Packet loss | Loss or missing telemetry | Dynamic | | | | | | |
| Agent killed | Agent crash | Baseline | | | | | | |
| Agent killed | Agent crash | Dynamic | | | | | | |
| Link down | Host or link down | Baseline | | | | | | |
| Link down | Host or link down | Dynamic | | | | | | |

## Repository Structure

```
README.md        install + run steps
project-plan.md
topology/        Mininet topology script
agents/          host telemetry agent
faults/          fault injection scripts
controller/      Ryu app: collector, poller, detector, responses
docs/            report, architecture diagram, slides
logs/            saved test runs and screenshots
```

## Ground Rules

- Agree on the telemetry format before writing code, and keep it updated in this file.
- Push code to the shared GitHub repo, with small and frequent commits.
- Post blockers in the group chat early instead of waiting.
- Keep `main` working; tag a tested version before Person C runs the final test runs.
