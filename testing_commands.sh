#!/usr/bin/env bash
# =============================================================================
# CN-Project — Full Testing Command Reference
# Run this on Ubuntu (Linux) with Mininet, Ryu, iperf, and iproute2 installed.
# =============================================================================
#
# USAGE:
#   Read through this file and run each section manually in separate terminals.
#   OR run the automated test suite at the bottom (Section 5).
#
# =============================================================================

# =============================================================================
# SECTION 0: PREREQUISITES — Install everything needed (run once)
# =============================================================================

# Update and install system packages
sudo apt update && sudo apt install -y \
    mininet \
    openvswitch-switch \
    iperf \
    iproute2 \
    python3-pip \
    git

# Install Python dependencies
pip3 install ryu psutil

# Verify installations
mn --version          # should print Mininet version
ryu-manager --version # should print Ryu version
iperf --version       # should print iperf version
python3 -c "import psutil; print('psutil OK')"

# Clone the repo (skip if already cloned)
# git clone <your-repo-url> CN-Project
# cd CN-Project


# =============================================================================
# SECTION 1: MANUAL RUN — Baseline Mode (3 terminals needed)
# =============================================================================

# --- TERMINAL 1: Start Ryu controller in BASELINE mode ---
ryu-manager controller/sdn_controller.py --mode=baseline
# Expected output:
#   [STARTUP] SDN Controller initialized. Mode: [BASELINE] ...
#   [COLLECTOR] UDP Telemetry Collector bound to 0.0.0.0:9999

# --- TERMINAL 2: Start Mininet topology ---
sudo python3 topology/topo.py
# Expected output:
#   *** Adding controller
#   *** Adding hosts: h1 h2 h3 h4 h5 h6
#   *** Adding switches: s1 s2 s3
#   *** Adding links: ...
#   mininet>

# --- AT THE mininet> PROMPT (Terminal 2): ---
agents start          # launches telemetry agents on all 6 hosts
pingall               # verify all hosts can reach each other (0% drop expected)

# Inject fault 1: UDP flood
fault flood h1 h4 20
# Wait 30 seconds, watch Terminal 1 for: [ANOMALY] [BASELINE] Congestion or flood detected
fault clear

# Inject fault 2: Added delay
fault delay h2 200
# Wait 10 seconds, watch Terminal 1 for: [ANOMALY] [BASELINE] Latency anomaly
fault clear

# Inject fault 3: Packet loss
fault loss h3 30
# Wait 10 seconds, watch Terminal 1 for: [ANOMALY] [BASELINE] Loss or missing telemetry
fault clear

# Inject fault 4: Kill agent
fault killagent h5
# Wait 8 seconds, watch Terminal 1 for: [ANOMALY] [BASELINE] Agent crash
fault clear

# Inject fault 5: Link down
fault linkdown s1 s2
# Wait 10 seconds, watch Terminal 1 for: [ANOMALY] [BASELINE] Host or link down
fault clear

agents stop
exit


# =============================================================================
# SECTION 2: MANUAL RUN — Dynamic Mode (3 terminals needed)
# =============================================================================

# --- TERMINAL 1: Start Ryu controller in DYNAMIC mode ---
ryu-manager controller/sdn_controller.py --mode=dynamic
# Expected: same startup, but responses will now install flow rules

# --- TERMINAL 2: Start Mininet topology ---
sudo python3 topology/topo.py

# --- AT THE mininet> PROMPT: ---
agents start
pingall

# Inject fault 1: UDP flood
fault flood h1 h4 20
# Expected: controller drops h1's flood (priority 200 rule), reroutes h2->h5 via s3
# Watch Terminal 1 for: [ACTION] DYNAMIC ACTION [CONGESTION]: Installed DROP flow rule
# Run in parallel (Terminal 3): iperf -c <h5-ip> -u -t 15   to see throughput recover
fault clear

# Inject fault 2: Added delay
fault delay h2 200
# Expected: controller logs latency anomaly, evaluates reroute
# Watch Terminal 1 for: [ACTION] DYNAMIC ACTION [REROUTE]
fault clear

# Inject fault 3: Packet loss
fault loss h3 30
# Expected: controller detects seq gaps, reroutes via s3
# Watch Terminal 1 for: [ACTION] DYNAMIC ACTION [REROUTE]
fault clear

# Inject fault 4: Kill agent
fault killagent h5
# Expected: alert logged, existing flows preserved (no disruption)
# Watch Terminal 1 for: [ALERT] Agent crash detected on h5. Data-plane flows active.
fault clear

# Inject fault 5: Link down
fault linkdown s1 s2
# Expected: stale flows purged, new reroute via s3 installed
# Watch Terminal 1 for: [ACTION] DYNAMIC ACTION [HOST/LINK DOWN]: Purged stale flows
fault clear

agents stop
exit


# =============================================================================
# SECTION 3: MEASURING METRICS (run in Terminal 3 during fault injection)
# =============================================================================

# Get h5 IP (run inside Mininet: h5 ip addr)
H5_IP="10.0.0.5"    # adjust to actual IP
H2_IP="10.0.0.2"

# Measure throughput with iperf (run on separate Mininet host via Terminal 3)
# Inside Mininet, open xterm for h2 and h5:
#   mininet> xterm h2 h5
# In h5's xterm:
iperf -s -u -i 1
# In h2's xterm (during fault injection):
iperf -c $H5_IP -u -b 10M -t 30 -i 1

# Measure latency with ping (inside Mininet):
#   mininet> h2 ping -i 0.5 h5
# You'll see latency spike during fault injection and recover in dynamic mode.

# Check controller logs in real time:
tail -f logs/controller.log

# Check fault events log:
cat logs/fault_events.log


# =============================================================================
# SECTION 4: VERIFY FLOW RULES INSTALLED BY CONTROLLER
# =============================================================================

# Dump flows on all switches (run in a separate terminal while Mininet is running):
sudo ovs-ofctl -O OpenFlow13 dump-flows s1
sudo ovs-ofctl -O OpenFlow13 dump-flows s2
sudo ovs-ofctl -O OpenFlow13 dump-flows s3

# During dynamic mode flood mitigation, you should see a rule like:
#   priority=200,udp,in_port=1,dl_src=00:00:00:00:00:01 actions=drop

# During reroute, you should see:
#   priority=150,in_port=1,dl_dst=00:00:00:00:00:05 actions=output:4


# =============================================================================
# SECTION 5: AUTOMATED TEST RUNNER (recommended — runs everything above)
# =============================================================================

# This runs all 5 faults in both modes automatically and saves JSON results.
# Make sure Ryu is NOT running before starting this (test_runner starts it).

# Run full automated test suite (both modes):
sudo python3 faults/test_runner.py --mode both --outdir logs/

# Run baseline only:
sudo python3 faults/test_runner.py --mode baseline --outdir logs/

# Run dynamic only:
sudo python3 faults/test_runner.py --mode dynamic --outdir logs/

# Run a dry-run (no real Mininet, just checks the script works):
sudo python3 faults/test_runner.py --mode both --dry-run

# After the run, check results:
cat logs/results_summary.json
ls logs/run_*.json


# =============================================================================
# SECTION 6: UPDATE THE TEST REPORT WITH REAL VALUES
# =============================================================================

# After running the automated tests, real JSON files will be in logs/.
# Open logs/test_report.md and replace every [SIM] value with the real
# numbers from logs/results_summary.json.
#
# Quick way to see all simulated values that need replacing:
grep -n "\[SIM\]" logs/test_report.md

# Once replaced, commit the real results:
git add logs/
git commit -m "results(navaneeth): live test results — real measured values"
git push origin main


# =============================================================================
# SECTION 7: QUICK SANITY CHECKS (run before full testing)
# =============================================================================

# Check OVS is running:
sudo systemctl status openvswitch-switch

# Check Ryu can import the controller without errors:
python3 -c "import ryu; print('Ryu OK')"
python3 -c "
import ast, sys
with open('controller/sdn_controller.py') as f:
    ast.parse(f.read())
print('controller/sdn_controller.py — syntax OK')
"

# Check agent syntax:
python3 -m py_compile agents/agent.py && echo "agents/agent.py — OK"

# Check faults syntax:
python3 -m py_compile faults/faults.py && echo "faults/faults.py — OK"
python3 -m py_compile faults/test_runner.py && echo "faults/test_runner.py — OK"

# Check topology syntax:
python3 -m py_compile topology/topo.py && echo "topology/topo.py — OK"

# =============================================================================
# END OF TESTING COMMANDS
# =============================================================================
