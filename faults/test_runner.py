#!/usr/bin/env python3
"""Automated Test Runner for SDN Distributed Network Monitoring Project.

Runs fault scenarios in baseline and dynamic mode against the Mininet topology,
captures telemetry/controller logs, computes detection times, and saves results.

Scenarios:
  1. fault flood h1 h4 20   - UDP flood
  2. fault delay h2 200      - 200ms delay
  3. fault loss h3 30        - 30% packet loss
  4. fault killagent h5      - kill telemetry agent
  5. fault linkdown s1 s2    - link down
Each followed by `fault clear`.

Usage:
  sudo python3 faults/test_runner.py --mode both --outdir logs/
  sudo python3 faults/test_runner.py --mode dynamic --detection-window 30
  python3 faults/test_runner.py --dry-run
"""

import argparse
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))
DEFAULT_LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")
DEFAULT_TOPO_SCRIPT = os.path.join(PROJECT_ROOT, "topology", "topo.py")
DEFAULT_CONTROLLER_SCRIPT = os.path.join(PROJECT_ROOT, "controller", "sdn_controller.py")
DEFAULT_FAULT_LOG = os.path.join(PROJECT_ROOT, "logs", "fault_events.log")
DEFAULT_CONTROLLER_LOG = os.path.join(PROJECT_ROOT, "logs", "controller.log")

FAULT_SCENARIOS = [
    {
        "id": "flood",
        "name": "UDP flood",
        "command": "fault flood h1 h4 20",
        "expected_detection": "Congestion or flood",
        "keywords": [
            "congestion", "flood", "high throughput", "rate-limit",
            "rate limit", "drop", "throughput", "udp flood"
        ],
        "description": "UDP flood between h1 and h4 (50 Mbps) for 20 seconds",
    },
    {
        "id": "delay",
        "name": "Added delay",
        "command": "fault delay h2 200",
        "expected_detection": "Latency anomaly",
        "keywords": [
            "latency", "delay", "high latency", "rtt", "lag"
        ],
        "description": "200ms link delay injected on host h2 via tc netem",
    },
    {
        "id": "loss",
        "name": "Packet loss",
        "command": "fault loss h3 30",
        "expected_detection": "Loss or missing telemetry",
        "keywords": [
            "loss", "packet loss", "packet_loss", "dropped", "error_count",
            "timeouts", "probe error"
        ],
        "description": "30% packet loss injected on host h3 via tc netem",
    },
    {
        "id": "killagent",
        "name": "Agent killed",
        "command": "fault killagent h5",
        "expected_detection": "Agent crash",
        "keywords": [
            "agent crash", "crash", "missing telemetry", "telemetry missing",
            "killagent", "agent stopped", "agent timeout", "silent"
        ],
        "description": "Telemetry agent terminated on host h5",
    },
    {
        "id": "linkdown",
        "name": "Link down",
        "command": "fault linkdown s1 s2",
        "expected_detection": "Host or link down",
        "keywords": [
            "link down", "linkdown", "host or link down", "port down",
            "link status down", "reroute", "alternate path", "topology change"
        ],
        "description": "Switch-switch link s1-s2 disabled in Mininet",
    },
]


def log_runner(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [test_runner] {msg}", flush=True)


def parse_timestamp_from_string(raw_ts):
    """Attempt to parse epoch float or date/time string to unix epoch timestamp."""
    try:
        return float(raw_ts)
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S,%f",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            dt = datetime.datetime.strptime(raw_ts, fmt)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def read_file_tail(filepath, max_lines=50):
    """Read the last N lines of a file safely."""
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            return [line.rstrip() for line in lines[-max_lines:]]
    except Exception as e:
        log_runner(f"Warning: Could not read {filepath}: {e}")
        return []


def read_new_lines_from_file(filepath, start_pos):
    """Read new lines appended to filepath since start_pos."""
    if not os.path.exists(filepath):
        return [], 0
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            f.seek(start_pos)
            lines = f.readlines()
            new_pos = f.tell()
            return [line.rstrip() for line in lines], new_pos
    except Exception as e:
        log_runner(f"Warning: Could not read new lines from {filepath}: {e}")
        return [], start_pos


def extract_fault_timestamp(fault_lines, scenario_cmd):
    """Find the exact fault timestamp recorded in fault_events.log."""
    cmd_parts = scenario_cmd.split()
    kind = cmd_parts[1] if len(cmd_parts) > 1 else ""

    for line in reversed(fault_lines):
        line = line.strip()
        if not line:
            continue
        # Format in faults.py: "%.3f %s" % (time.time(), text)
        m = re.match(r"^(\d+\.\d+|\d+)\s+(.+)$", line)
        if m:
            ts = float(m.group(1))
            event_text = m.group(2).lower()
            if kind.lower() in event_text:
                return ts, line

    # Fallback to last line if any
    if fault_lines:
        m = re.match(r"^(\d+\.\d+|\d+)\s+(.+)$", fault_lines[-1].strip())
        if m:
            return float(m.group(1)), fault_lines[-1].strip()

    return None, None


def parse_controller_anomaly(controller_lines, fault_ts, scenario):
    """Parse detection time and anomaly text from controller log lines."""
    best_detection = None
    best_dt = None
    recovery_ts = None
    action_text = None

    ts_patterns = [
        re.compile(r"^\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\]?\s*(.*)$"),
        re.compile(r"^\[?(\d{10}(?:\.\d+)?)\]?\s*(.*)$"),
    ]

    # Only explicit scenario-specific anomaly labels count as detection.
    expected_tags = {
        "flood": ("[congestion/flood]",),
        "delay": ("[host/app problem]", "[congestion/flood]"),
        "loss": ("[packet loss]",),
        "killagent": ("[agent crash]",),
        "linkdown": ("[host/link down]",),
    }.get(scenario["id"], ())

    for line in controller_lines:
        line_clean = line.strip()
        if not line_clean:
            continue

        line_ts = None
        content = line_clean

        for pat in ts_patterns:
            m = pat.match(line_clean)
            if m:
                parsed = parse_timestamp_from_string(m.group(1))
                if parsed:
                    line_ts = parsed
                    content = m.group(2)
                    break

        line_lower = content.lower()
        is_expected_anomaly = any(tag in line_lower for tag in expected_tags)

        if is_expected_anomaly and best_detection is None and line_ts is not None and fault_ts is not None:
            if line_ts >= (fault_ts - 0.2):
                best_dt = max(0.0, line_ts - fault_ts)
                best_detection = (line_ts, content)

        # Action time is recorded separately; it is not proof of service recovery.
        if "[action]" in line_lower and line_ts is not None and fault_ts is not None:
            if line_ts >= fault_ts:
                recovery_ts = line_ts
                action_text = content

    return best_detection, best_dt, recovery_ts, action_text


def is_controller_listening(host="127.0.0.1", port=6653):
    """Check if Ryu controller is listening on OpenFlow port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


class MininetSubprocessRunner:
    """Manages executing the Mininet topology script and injecting commands via stdin."""

    def __init__(self, topo_script, repo_root):
        self.topo_script = topo_script
        self.repo_root = repo_root
        self.proc = None
        self.stdout_lines = []
        self._reader_thread = None
        self._stop_reader = False

    def _reader(self):
        while not self._stop_reader and self.proc and self.proc.stdout:
            try:
                line = self.proc.stdout.readline()
                if not line:
                    break
                self.stdout_lines.append(line.rstrip())
            except Exception:
                break

    def start(self):
        cmd = []
        if os.name != "nt":
            # On Linux, Mininet requires root
            if os.geteuid() != 0:
                cmd.append("sudo")
        cmd.extend([sys.executable, self.topo_script])

        log_runner(f"Starting Mininet process: {' '.join(cmd)}")
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=self.repo_root,
            env=env,
        )

        self._stop_reader = False
        self._reader_thread = threading.Thread(target=self._reader, daemon=True)
        self._reader_thread.start()

    def send_command(self, cmd_str, wait_sec=1.0):
        if not self.proc or self.proc.poll() is not None:
            log_runner(f"Warning: Mininet process is not running, cannot send '{cmd_str}'")
            return
        log_runner(f"Mininet CLI << {cmd_str}")
        try:
            self.proc.stdin.write(cmd_str + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            log_runner(f"Error writing to Mininet stdin: {e}")
        if wait_sec > 0:
            time.sleep(wait_sec)

    def stop(self):
        if self.proc:
            try:
                if self.proc.poll() is None:
                    self.send_command("exit", wait_sec=0.5)
                    self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log_runner("Mininet did not exit gracefully; terminating...")
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except Exception as e:
                log_runner(f"Error stopping Mininet process: {e}")

        self._stop_reader = True
        self.proc = None

        # Clean up any leftover Mininet state on Linux
        if os.name != "nt":
            try:
                subprocess.run(
                    ["sudo", "mn", "-c"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except Exception:
                pass


def simulate_dry_run(scenario, mode, outdir, fault_log_path, controller_log_path):
    """Simulates a test run without requiring root/Mininet VM (for verification)."""
    log_runner(f"[DRY-RUN] Simulating scenario: {scenario['name']} (mode: {mode})")
    time.sleep(0.3)
    t_fault = time.time()

    # Write mock fault event
    os.makedirs(os.path.dirname(fault_log_path), exist_ok=True)
    with open(fault_log_path, "a", encoding="utf-8") as f:
        f.write(f"{t_fault:.3f} {scenario['command'].replace('fault ', '')}\n")

    # Write mock controller detection
    detect_dt = 1.45 if mode == "dynamic" else 1.82
    t_detect = t_fault + detect_dt
    t_str = datetime.datetime.fromtimestamp(t_detect).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    with open(controller_log_path, "a", encoding="utf-8") as f:
        f.write(f"{t_str} [ANOMALY] [{mode.upper()}] {scenario['expected_detection']} detected on target\n")
        if mode == "dynamic":
            f.write(f"{t_str} [ACTION] Mitigation flow rule installed for {scenario['id']}\n")

    time.sleep(0.2)
    # Write mock fault clear
    t_clear = time.time()
    with open(fault_log_path, "a", encoding="utf-8") as f:
        f.write(f"{t_clear:.3f} clear\n")

    return {
        "fault_timestamp": t_fault,
        "detection_timestamp": t_detect,
        "detection_time": round(detect_dt, 3),
        "detected": True,
        "actual_detection": f"{scenario['expected_detection']} detected on target",
        "recovery_time": round(detect_dt + 0.85, 3) if mode == "dynamic" else None,
        "stdout_tail": ["[DRY-RUN] Simulation successful"],
    }


def run_single_scenario(
    scenario,
    mode,
    topo_script,
    repo_root,
    fault_log_path,
    controller_log_path,
    detection_window=30,
    dry_run=False,
):
    """Executes one fault scenario under the given mode and collects metrics."""
    run_start_time = time.time()
    run_timestamp_iso = datetime.datetime.now().isoformat()
    log_runner("=" * 65)
    log_runner(f"SCENARIO: {scenario['name'].upper()} | MODE: {mode.upper()}")
    log_runner(f"Command : {scenario['command']}")
    log_runner(f"Expected: {scenario['expected_detection']}")
    log_runner(f"Started : {run_timestamp_iso}")
    log_runner("=" * 65)

    os.makedirs(os.path.dirname(fault_log_path), exist_ok=True)
    os.makedirs(os.path.dirname(controller_log_path), exist_ok=True)

    # Note current file sizes to only parse new lines
    fault_start_pos = os.path.getsize(fault_log_path) if os.path.exists(fault_log_path) else 0
    controller_start_pos = os.path.getsize(controller_log_path) if os.path.exists(controller_log_path) else 0

    if dry_run:
        sim = simulate_dry_run(scenario, mode, DEFAULT_LOGS_DIR, fault_log_path, controller_log_path)
        fault_ts = sim["fault_timestamp"]
        detect_ts = sim["detection_timestamp"]
        detect_dt = sim["detection_time"]
        detected = sim["detected"]
        actual_detection = sim["actual_detection"]
        recovery_time = sim["recovery_time"]
        mininet_output = sim["stdout_tail"]
    else:
        mn = MininetSubprocessRunner(topo_script, repo_root)
        try:
            mn.start()
            log_runner("Waiting 4 seconds for topology & NAT initialization...")
            time.sleep(4)

            # Start host telemetry agents
            log_runner("Starting host telemetry agents...")
            mn.send_command("agents start", wait_sec=3)

            # Record offset right before injection
            if os.path.exists(fault_log_path):
                fault_start_pos = os.path.getsize(fault_log_path)
            if os.path.exists(controller_log_path):
                controller_start_pos = os.path.getsize(controller_log_path)

            # Inject fault
            inject_time_est = time.time()
            log_runner(f"Injecting fault: '{scenario['command']}'")
            mn.send_command(scenario["command"], wait_sec=1)

            # Wait for detection window
            log_runner(f"Waiting for detection window ({detection_window}s)...")
            detected_early = False
            detect_check_start = time.time()
            while time.time() - detect_check_start < detection_window:
                time.sleep(1.0)
                # Quick inspection of controller log during the window
                ctrl_lines, _ = read_new_lines_from_file(controller_log_path, controller_start_pos)
                det, dt, _, _ = parse_controller_anomaly(ctrl_lines, inject_time_est, scenario)
                if det and not detected_early:
                    log_runner(f"Anomaly observed in controller log! Time-to-detect ~ {dt:.2f}s")
                    detected_early = True

            # Clear fault
            log_runner("Clearing fault condition ('fault clear')...")
            mn.send_command("fault clear", wait_sec=2)

            # Stop agents and exit
            log_runner("Stopping agents...")
            mn.send_command("agents stop", wait_sec=1)

        finally:
            log_runner("Stopping Mininet...")
            mn.stop()
            mininet_output = mn.stdout_lines

        # Collect new log lines
        new_fault_lines, _ = read_new_lines_from_file(fault_log_path, fault_start_pos)
        new_ctrl_lines, _ = read_new_lines_from_file(controller_log_path, controller_start_pos)

        # Parse fault timestamp from fault_events.log
        parsed_fault_ts, _ = extract_fault_timestamp(new_fault_lines, scenario["command"])
        fault_ts = parsed_fault_ts if parsed_fault_ts is not None else inject_time_est

        # Parse detection from controller log
        det_entry, detect_dt, rec_ts, act_txt = parse_controller_anomaly(new_ctrl_lines, fault_ts, scenario)
        if det_entry:
            detected = True
            detect_ts = det_entry[0]
            actual_detection = det_entry[1]
        else:
            detected = False
            detect_ts = None
            detect_dt = None
            actual_detection = "No anomaly detected within window"

        # This is controller action latency, not measured service recovery.
        action_time = round(rec_ts - fault_ts, 3) if (rec_ts is not None and fault_ts is not None) else None

    run_end_time = time.time()
    log_runner(f"Result: Detected={detected} | Time-to-detect={detect_dt}s | Actual='{actual_detection}'")

    result = {
        "scenario": scenario["id"],
        "scenario_name": scenario["name"],
        "mode": mode,
        "command": scenario["command"],
        "expected_detection": scenario["expected_detection"],
        "actual_detection": actual_detection,
        "detected": detected,
        "time_to_detect_sec": detect_dt,
        "time_to_action_sec": action_time if not dry_run else None,
        "time_to_recover_sec": None,  # Requires independent traffic/RTT/loss measurements.
        "timestamps": {
            "run_started": run_timestamp_iso,
            "fault_injected": fault_ts,
            "detected": detect_ts,
            "run_ended": datetime.datetime.now().isoformat(),
            "duration_sec": round(run_end_time - run_start_time, 2),
        },
        "logs": {
            "fault_events_tail": read_file_tail(fault_log_path, max_lines=20),
            "controller_log_tail": read_file_tail(controller_log_path, max_lines=30),
            "mininet_stdout_tail": mininet_output[-25:] if mininet_output else [],
        },
    }

    return result


def print_summary_table(results):
    """Print an aligned ASCII summary table of test results."""
    headers = [
        "Fault Injected",
        "Mode",
        "Expected Detection",
        "Actual Detection",
        "Time to Detect",
        "Time to Recover (measured)",
        "Status",
    ]

    rows = []
    for r in results:
        detect_str = f"{r['time_to_detect_sec']:.2f}s" if r["time_to_detect_sec"] is not None else "N/A"
        recover_str = (
            f"{r['time_to_recover_sec']:.2f}s"
            if r.get("time_to_recover_sec") is not None
            else "Not measured"
        )
        status_str = "PASS" if r["detected"] else "FAIL"

        # Truncate strings if too wide for the table
        act = r["actual_detection"]
        if len(act) > 32:
            act = act[:29] + "..."

        rows.append([
            r["scenario_name"],
            r["mode"],
            r["expected_detection"],
            act,
            detect_str,
            recover_str,
            status_str,
        ])

    col_widths = [len(h) for h in headers]
    for row in rows:
        for idx, val in enumerate(row):
            col_widths[idx] = max(col_widths[idx], len(str(val)))

    separator = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"

    print("\n" + "=" * len(separator))
    print("                    AUTOMATED SDN TEST RESULTS SUMMARY")
    print("=" * len(separator))
    print(separator)

    header_line = "|" + "|".join(f" {headers[i].ljust(col_widths[i])} " for i in range(len(headers))) + "|"
    print(header_line)
    print(separator)

    for row in rows:
        row_line = "|" + "|".join(f" {str(row[i]).ljust(col_widths[i])} " for i in range(len(row))) + "|"
        print(row_line)

    print(separator)
    passed_count = sum(1 for r in results if r["detected"])
    total_count = len(results)
    print(f"Total Scenarios Run: {total_count} | Detected: {passed_count}/{total_count}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Automated Fault Test Runner for Distributed Network Monitoring SDN Project"
    )
    parser.add_argument(
        "--mode",
        choices=["baseline", "dynamic", "both"],
        default="both",
        help="Controller mode to evaluate (default: both)",
    )
    parser.add_argument(
        "--outdir",
        default=DEFAULT_LOGS_DIR,
        help=f"Directory to save JSON results (default: {DEFAULT_LOGS_DIR})",
    )
    parser.add_argument(
        "--detection-window",
        type=int,
        default=30,
        help="Detection window in seconds per fault scenario (default: 30)",
    )
    parser.add_argument(
        "--topo",
        default=DEFAULT_TOPO_SCRIPT,
        help="Path to Mininet topology script (default: topology/topo.py)",
    )
    parser.add_argument(
        "--fault-log",
        default=DEFAULT_FAULT_LOG,
        help="Path to fault events log (default: logs/fault_events.log)",
    )
    parser.add_argument(
        "--controller-log",
        default=DEFAULT_CONTROLLER_LOG,
        help="Path to controller log (default: logs/controller.log)",
    )
    parser.add_argument(
        "--scenarios",
        default="all",
        help="Comma-separated scenario IDs to run (flood,delay,loss,killagent,linkdown) or 'all'",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate execution without Mininet/sudo (useful for offline testing/verification)",
    )

    args = parser.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    # Determine modes to run
    modes = ["baseline", "dynamic"] if args.mode == "both" else [args.mode]

    # Filter scenarios if requested
    if args.scenarios == "all":
        selected_scenarios = FAULT_SCENARIOS
    else:
        chosen_ids = [s.strip().lower() for s in args.scenarios.split(",")]
        selected_scenarios = [s for s in FAULT_SCENARIOS if s["id"].lower() in chosen_ids]
        if not selected_scenarios:
            log_runner(f"Error: No valid scenarios found matching '{args.scenarios}'")
            sys.exit(1)

    log_runner("Starting Automated SDN Test Runner")
    log_runner(f"Output directory : {args.outdir}")
    log_runner(f"Selected modes   : {modes}")
    log_runner(f"Scenarios count  : {len(selected_scenarios)}")
    log_runner(f"Detection window : {args.detection_window}s")

    if not args.dry_run:
        if args.mode == "both":
            parser.error(
                "Live --mode both is unsafe: this runner cannot restart/reconfigure Ryu. "
                "Run one batch at a time and restart Ryu with the matching --mode."
            )
        if not is_controller_listening():
            parser.error(
                "Ryu is not listening on 127.0.0.1:6653. Start it first with "
                "'ryu-manager controller/sdn_controller.py --mode=<baseline|dynamic>'."
            )
        log_runner("Verified: Controller is listening on port 6653.")

        # Confirm the latest startup marker belongs to the requested controller mode.
        controller_lines = read_file_tail(args.controller_log, max_lines=500)
        mode_markers = [
            line for line in controller_lines
            if "SDN Controller initialized. Mode:" in line
        ]
        if not mode_markers:
            parser.error(
                f"No controller startup/mode marker found in {args.controller_log}; "
                "check the controller log path and restart Ryu."
            )
        latest_mode = re.search(r"Mode:\s*\[(BASELINE|DYNAMIC)\]", mode_markers[-1], re.I)
        actual_mode = latest_mode.group(1).lower() if latest_mode else "unknown"
        if actual_mode != args.mode:
            parser.error(
                f"Requested {args.mode} batch but controller log reports {actual_mode}. "
                "Restart Ryu with the matching --mode before running."
            )

    all_results = []

    for mode in modes:
        log_runner(f"\n>>>> STARTING TEST BATCH FOR MODE: {mode.upper()} <<<<\n")
        for sc in selected_scenarios:
            res = run_single_scenario(
                scenario=sc,
                mode=mode,
                topo_script=args.topo,
                repo_root=PROJECT_ROOT,
                fault_log_path=args.fault_log,
                controller_log_path=args.controller_log,
                detection_window=args.detection_window,
                dry_run=args.dry_run,
            )
            all_results.append(res)

            # Save individual run file: logs/run_<scenario>_<mode>_<timestamp>.json
            ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            run_filename = f"run_{sc['id']}_{mode}_{ts_str}.json"
            run_filepath = os.path.join(args.outdir, run_filename)
            try:
                with open(run_filepath, "w", encoding="utf-8") as f:
                    json.dump(res, f, indent=2)
                log_runner(f"Saved run record: {run_filepath}")
            except Exception as e:
                log_runner(f"Error saving run file {run_filepath}: {e}")

            time.sleep(1.0)

    # Save summary file: logs/results_summary.json
    summary_path = os.path.join(args.outdir, "results_summary.json")
    summary_data = {
        "generated_at": datetime.datetime.now().isoformat(),
        "total_runs": len(all_results),
        "passed_detections": sum(1 for r in all_results if r["detected"]),
        "modes_tested": modes,
        "runs": all_results,
    }
    try:
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary_data, f, indent=2)
        log_runner(f"Saved results summary: {summary_path}")
    except Exception as e:
        log_runner(f"Error saving summary file {summary_path}: {e}")

    # Print summary table
    print_summary_table(all_results)
    log_runner("Test execution completed successfully.")


if __name__ == "__main__":
    main()
