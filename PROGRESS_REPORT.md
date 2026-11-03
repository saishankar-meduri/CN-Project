# CN-Project — Progress Report
> Last updated: 2026-10-14

---

## Overall Completion: ✅ 100% (Code & Docs)

| Area | Status |
|------|--------|
| Sai's tasks (topology, agent, faults, test runner) | ✅ 5/5 done |
| Chinmayi's tasks (Ryu controller, all 7 sub-components) | ✅ 7/7 done |
| Navaneeth's tasks (README, report, architecture, slides, logs) | ✅ 5/5 done |
| Live integration testing (requires Linux + Mininet) | ⏳ Pending — run `testing_commands.sh` on Ubuntu |

---

## Files in the Repository

| File | Owner | Size | Notes |
|------|-------|------|-------|
| `topology/topo.py` | Sai | 1.8 KB | Triangle topology — s1/s2/s3, 6 hosts, 10 Mbps links |
| `agents/agent.py` | Sai | 3.4 KB | UDP telemetry agent with echo server + psutil metrics |
| `faults/faults.py` | Sai | 3.6 KB | 5 fault types + agent launcher as Mininet CLI commands |
| `faults/test_runner.py` | Sai | 25 KB | Automated test runner (all faults × both modes) |
| `controller/sdn_controller.py` | Chinmayi | 39.4 KB | Full Ryu app — 970 lines |
| `README.md` | Navaneeth | 27.9 KB | Install + run guide |
| `docs/architecture.md` | Navaneeth | 24.6 KB | ASCII diagrams, data flow, sequence diagrams |
| `docs/report.md` | Navaneeth | 25.7 KB | Academic report (~1500 words, all rubric sections) |
| `docs/slides.md` | Navaneeth | 14.8 KB | 15 presentation slides |
| `logs/test_report.md` | Navaneeth | ~18 KB | Test report (values simulated — replace after live run) |
| `logs/results_summary.json` | Navaneeth | 18.8 KB | Dry-run summary (replace after live run) |
| `project-plan.md` | All | 8.2 KB | All 17 checkboxes ticked ✅ |

---

## What Each File Does

### `topology/topo.py`
- Builds a triangle of 3 OVS switches (s1, s2, s3) with 10 Mbps inter-switch TCLinks
- Attaches 6 hosts: h1,h2 → s1 | h3,h4 → s2 | h5,h6 → s3
- Adds a NAT node at `10.0.0.254` so agents can reach the controller
- Connects to remote Ryu at `127.0.0.1:6653` over OpenFlow 1.3
- Extends Mininet CLI with `FaultCLI` (agent launch + fault injection commands)

### `agents/agent.py`
- Runs a UDP echo server on port 9998 (so peers can probe it for RTT)
- Every 1 second: probes its peer host, measures RTT, reads CPU/memory via psutil
- Sends one JSON datagram to the controller at `10.0.0.254:9999`
- Keeps running even if the network goes down (controller sees missing telemetry)

### `faults/faults.py`
- Adds `agents start` / `agents stop` to the Mininet CLI
- Adds `fault <type>` with 5 sub-commands:

| Command | What it does |
|---------|-------------|
| `fault flood h1 h4 [secs]` | iperf UDP flood at 50 Mbps |
| `fault delay h1 200` | tc netem 200ms delay on h1-eth0 |
| `fault loss h3 30` | tc netem 30% packet loss on h3-eth0 |
| `fault killagent h5` | kills that host's agent process |
| `fault linkdown s1 s2` | brings the s1-s2 switch link down |
| `fault clear` | undoes all of the above |

### `faults/test_runner.py`
- Automates running all 5 fault scenarios in both baseline and dynamic mode
- Spawns Mininet, sends CLI commands via stdin, waits for detection window
- Parses `logs/fault_events.log` and `logs/controller.log` for detection timestamps
- Saves per-run JSON to `logs/run_<scenario>_<mode>_<timestamp>.json`
- Generates `logs/results_summary.json` and prints ASCII summary table

### `controller/sdn_controller.py`
- **Learning switch**: MAC table per datapath, installs forwarding rules via OFPFlowMod
- **UDP collector** (threaded, port 9999): receives telemetry, checks seq gaps, handles bad JSON
- **Stats poller**: every 5s sends OFPFlowStatsRequest + OFPPortStatsRequest to all switches
- **Anomaly detector**: 4 rules correlating telemetry + flow stats:

| Condition | Detection | Response (dynamic only) |
|-----------|-----------|------------------------|
| Latency > 100ms + throughput > 8 Mbps | CONGESTION/FLOOD | Drop offending MAC (priority 200, 30s), reroute via alternate switch |
| Latency > 100ms + throughput normal | HOST/APP PROBLEM | Log only |
| Telemetry missing > 5s + flows active | AGENT CRASH | Alert, keep flows |
| Telemetry missing > 5s + no flows | HOST/LINK DOWN | Delete stale flows, reroute |

- **Mode flag**: `--mode=baseline` (log only) or `--mode=dynamic` (install flow rules)
- **Logs**: to stdout + `logs/controller.log` with epoch timestamps

---

## Task Checklist (All Done ✅)

### Sai
- [x] Mininet topology (triangle, 6 hosts, TCLink)
- [x] Host telemetry agent (UDP echo + JSON metrics)
- [x] Fault injection scripts (5 types)
- [x] Automated test runner (baseline + dynamic, saves JSON)
- [x] Shared topology and agent with team

### Chinmayi
- [x] Ryu learning switch (OpenFlow 1.3)
- [x] Threaded UDP collector (malformed JSON, seq gap detection)
- [x] Periodic flow + port stats polling
- [x] Anomaly detection (4 correlation rules)
- [x] Dynamic responses (drop/rate-limit, reroute, flow cleanup)
- [x] Baseline/dynamic mode flag (`--mode`)
- [x] Logging to file + stdout with timestamps

### Navaneeth
- [x] Architecture diagram (`docs/architecture.md`)
- [x] Full academic report (`docs/report.md`)
- [x] README with install + run steps
- [x] Test logs and results table (`logs/test_report.md`) — *values simulated, update after live run*
- [x] Presentation slides (`docs/slides.md`)

---

## D1 / D2 Milestone Status

| # | Milestone | Status |
|---|-----------|--------|
| 1 | Mininet + Ryu running, basic switching | ✅ Code complete |
| 2 | Host sends telemetry, controller prints it | ✅ Code complete |
| 3 | Flow stats polling | ✅ Code complete |
| 4 | Basic detection rules | ✅ Code complete |
| 5 | Architecture diagram + D1 demo ready | ✅ Complete |
| 6 | Dynamic responses (drop, reroute) | ✅ Code complete |
| 7 | All fault scenarios tested (baseline + dynamic) | ⏳ Run `testing_commands.sh` on Linux |
| 8 | Results table and graphs | ⏳ Fill in after live run |
| 9 | Report, README, slides, demo | ✅ Complete |
| 10 | Viva prep | ⚠️ Do a dry-run walkthrough together |

---

## Pending: Live Testing

All code is complete. Real test metrics need to be captured on a **Linux machine with Mininet**.

See `testing_commands.sh` for the exact commands to run.  
After running, update `logs/test_report.md` by replacing all `[SIM]` values with real numbers.

---

## Git Push Order

| Order | Person | Files to Push |
|-------|--------|--------------|
| **1st** | **Sai** | `topology/`, `agents/`, `faults/`, `project-plan.md`, `PROGRESS_REPORT.md` |
| **2nd** | **Chinmayi** | `controller/sdn_controller.py` |
| **3rd** | **Navaneeth** | `README.md`, `docs/`, `logs/` |
