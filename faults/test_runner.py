#!/usr/bin/env python3
"""Automated Test Runner for SDN Distributed Network Monitoring Project.

Runs fault scenarios in baseline and dynamic mode against the Mininet topology,
captures telemetry/controller logs, computes detection times, and saves results.

Scenarios:
  1. fault flood h1 h4 20   - UDP flood
  2. fault delay h2 200      - 200ms delay
  3. fault loss h3 30        - 30% packet loss
  4. fault killagent h5      - kill telemetry agent
  5. fault linkdown h1 s1    - link down
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
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))
DEFAULT_LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")
DEFAULT_TOPO_SCRIPT = os.path.join(PROJECT_ROOT, "topology", "topo.py")
DEFAULT_CONTROLLER_SCRIPT = os.path.join(PROJECT_ROOT, "controller", "sdn_controller.py")
DEFAULT_AGENT_SCRIPT = os.path.join(PROJECT_ROOT, "agents", "agent.py")
DEFAULT_FAULT_LOG = os.path.join(PROJECT_ROOT, "logs", "fault_events.log")
DEFAULT_CONTROLLER_LOG = os.path.join(PROJECT_ROOT, "logs", "controller.log")
DEFAULT_CONTROLLER_PYTHON = os.path.join(PROJECT_ROOT, ".venv", "bin", "python")
MEASUREMENT_INTERVAL_SEC = 1.0
RECOVERY_WINDOW_SEC = 30.0
RECOVERY_CONSECUTIVE_SAMPLES = 2
RUNNER_ACK_RE = re.compile(r"^RUNNER_ACK=([0-9a-f]{32})$")

FAULT_SCENARIOS = [
    {
        "id": "flood",
        "probe": ("h1", "h4"),
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
        "probe": ("h2", "h5"),
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
        "probe": ("h3", "h6"),
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
        "probe": ("h5", "h2"),
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
        "probe": ("h1", "h4"),
        "name": "Link down",
        "command": "fault linkdown h1 s1",
        "expected_detection": "Host or link down",
        "keywords": [
            "link down", "linkdown", "host or link down", "port down",
            "link status down", "reroute", "alternate path", "topology change"
        ],
        "description": "Access link h1-s1 disabled in Mininet",
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


def validate_runner_acknowledgments(lines, expected_token):
    """Validate complete Mininet ack lines; return True only for one exact ack."""
    acknowledgments = []
    for raw_line in lines:
        line = raw_line.strip()
        # cmd.Cmd writes its prompt before reading stdin, so the first output
        # line after a command can have this one prompt prefix.
        if line.startswith("mininet> "):
            line = line[len("mininet> "):]
        if line.startswith("RUNNER_ACK_ERROR="):
            raise ValueError("Mininet rejected the runner acknowledgment: " + line)
        if line.startswith("RUNNER_ACK"):
            match = RUNNER_ACK_RE.fullmatch(line)
            if not match:
                raise ValueError("malformed Mininet runner acknowledgment: " + line)
            acknowledgments.append(match.group(1))
    if len(acknowledgments) > 1:
        raise ValueError("duplicate Mininet runner acknowledgments")
    if acknowledgments and acknowledgments[0] != expected_token:
        raise ValueError("mismatched Mininet runner acknowledgment")
    return bool(acknowledgments)


def read_host_process_identity(pid):
    """Read a host-visible process identity without relying on its process group."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            argv = [part.decode("utf-8", "replace")
                    for part in f.read().split(b"\0") if part]
        with open("/proc/%d/stat" % pid, encoding="utf-8") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return {"argv": argv, "state": fields[0], "start_ticks": int(fields[19])}
    except (OSError, ValueError, IndexError):
        return None


def agent_record_matches(record, host, run_id, expected_host_pid, details,
                         agent_script=DEFAULT_AGENT_SCRIPT):
    """Match PID, namespace owner, host, run token, script, and process start."""
    if not isinstance(record, dict) or not details or details.get("state") == "Z":
        return False
    if (record.get("host") != host or record.get("run_id") != run_id or
            record.get("host_pid") != expected_host_pid or
            record.get("pid_scope") != "host" or
            record.get("command") != os.path.realpath(agent_script) or
            record.get("start_ticks") != details.get("start_ticks")):
        return False
    argv = details.get("argv", [])
    script_real = os.path.realpath(agent_script)
    script_present = any(arg.endswith(".py") and os.path.realpath(arg) == script_real
                         for arg in argv)
    return (script_present and "--id=%s" % host in argv
            and "--run-id=%s" % run_id in argv)


def read_file_tail(filepath, max_lines=50):
    """Read the last N lines of a file safely."""
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4 * 1024 * 1024))
            if size > 4 * 1024 * 1024:
                f.readline()  # discard a possibly partial first line
            lines = f.read().decode("utf-8", errors="replace").splitlines()
            return lines[-max_lines:]
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
    """Require one exact verified-injection event; cleanup/failure lines never match."""
    parts = scenario_cmd.split()
    if len(parts) < 3 or parts[0] != "fault":
        raise ValueError("invalid scenario command")
    expected = "FAULT_INJECTED " + " ".join(parts[1:])
    matches = []
    for line in fault_lines:
        match = re.match(r"^(\d+\.\d+|\d+)\s+(.+)$", line.strip())
        if match and match.group(2) == expected:
            matches.append((float(match.group(1)), line.strip()))
    if len(matches) != 1:
        raise ValueError("expected exactly one %r event, found %d" % (expected, len(matches)))
    return matches[0]


def verified_clear_event(fault_lines):
    return sum(1 for line in fault_lines
               if re.match(r"^\d+(?:\.\d+)?\s+FAULT_CLEARED$", line.strip())) == 1


def extract_cleanup_timestamp(cleanup_lines):
    matches = [float(match.group(1)) for line in cleanup_lines
               if (match := re.match(r"^(\d+(?:\.\d+)?)\s+FAULT_CLEARED$",
                                     line.strip()))]
    if len(matches) != 1:
        raise ValueError("expected exactly one verified cleanup timestamp, found %d" %
                         len(matches))
    return matches[0]


def parse_controller_anomaly(controller_lines, fault_ts, scenario):
    """Return first matching anomaly and only a host/category-attributable action."""
    best_detection = None
    best_dt = None
    action_ts = None
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

    parsed_lines = []
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

        parsed_lines.append((line_ts, content))
        line_lower = content.lower()
        is_expected_anomaly = any(tag in line_lower for tag in expected_tags) and (not scenario.get("probe") or (" on %s:" % scenario["probe"][0]) in line_lower)

        if is_expected_anomaly and best_detection is None and line_ts is not None and fault_ts is not None:
            if line_ts >= fault_ts:
                best_dt = max(0.0, line_ts - fault_ts)
                best_detection = (line_ts, content)

    affected_host = None
    if best_detection:
        host_match = re.search(r"\bon\s+(h[1-6])\b", best_detection[1], re.I)
        if host_match:
            affected_host = host_match.group(1).lower()
    if best_detection and affected_host:
        action_tag = {
            "flood": "[congestion]", "delay": "[congestion]", "loss": None,
            "killagent": "[agent crash]", "linkdown": "[host/link down]",
        }.get(scenario["id"])
        # Flood actions identify the selected offender by MAC; tie that MAC to the
        # host reported by the anomaly. Other actions must name the affected host.
        host_mac = None
        try:
            with open(DEFAULT_CONTROLLER_SCRIPT, encoding="utf-8") as source_file:
                controller_source = source_file.read()
            match = re.search(r'"%s":\s*\{"mac":\s*"([0-9a-f:]+)"' % affected_host,
                              controller_source, re.I)
            host_mac = match.group(1).lower() if match else None
        except OSError:
            pass
        for line_ts, content in parsed_lines:
            lower = content.lower()
            if not line_ts or line_ts < best_detection[0] or "[action]" not in lower:
                continue
            relevant = False
            if scenario["id"] == "flood":
                macs = set(re.findall(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", lower))
                relevant = ("[congestion]" in lower and host_mac is not None
                            and macs == {host_mac})
            elif scenario["id"] == "delay" and "[congestion/flood]" in best_detection[1].lower():
                macs = set(re.findall(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", lower))
                relevant = ("[congestion]" in lower and host_mac is not None
                            and macs == {host_mac})
            elif action_tag:
                named_hosts = set(re.findall(r"\bh[1-6]\b", lower))
                relevant = action_tag in lower and named_hosts == {affected_host}
            # No unhosted reroute or generic action is attributed to an anomaly.
            if relevant:
                action_ts, action_text = line_ts, content
                break

    return best_detection, best_dt, action_ts, action_text


def mode_run_directory(base, mode, run_id):
    """Create an isolated, collision-resistant output directory per mode/run."""
    path = os.path.join(base, mode, run_id)
    os.makedirs(path, exist_ok=False)
    return path


def file_sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_executable(value):
    if not value:
        return None
    if os.path.sep in value:
        path = os.path.abspath(value)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else None
    return shutil.which(value)


def verify_python_imports(interpreter, modules, role, required_version=None, prelude=""):
    """Verify an interpreter by importing modules in that exact executable."""
    interpreter = _resolve_executable(interpreter)
    if not interpreter:
        raise RuntimeError("%s interpreter is not an executable path" % role)
    imports = "\n".join("import importlib; importlib.import_module(%r)" % module
                          for module in modules)
    code = (prelude + "\n" + imports +
            "\nimport json,sys; print(json.dumps({'executable':sys.executable,"
            "'version':list(sys.version_info[:3])}))")
    try:
        result = subprocess.run([interpreter, "-c", code], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=12,
                                check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("%s interpreter verification failed: %s" % (role, exc)) from exc
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError("%s interpreter %s cannot import %s: %s" %
                           (role, interpreter, ", ".join(modules), details))
    try:
        metadata = json.loads(result.stdout.splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError("%s interpreter returned invalid verification output" % role) from exc
    if required_version and tuple(metadata["version"][:2]) != tuple(required_version):
        raise RuntimeError("%s interpreter must be Python %d.%d; got %s" %
                           (role, required_version[0], required_version[1],
                            ".".join(map(str, metadata["version"]))))
    return os.path.realpath(interpreter)


def select_topology_interpreter(configured=None):
    """Select and verify a Python that imports the installed Mininet package."""
    configured = configured or os.environ.get("SDN_TOPOLOGY_PYTHON")
    candidates = [configured] if configured else [
        sys.executable, shutil.which("python3"), shutil.which("python")]
    errors = []
    seen = set()
    for candidate in candidates:
        resolved = _resolve_executable(candidate)
        if not resolved or resolved in seen:
            continue
        seen.add(resolved)
        try:
            return verify_python_imports(resolved, ("mininet", "psutil"),
                                        "Mininet topology/agents")
        except RuntimeError as exc:
            errors.append(str(exc))
            if configured:
                break
    raise RuntimeError("no usable Mininet Python interpreter found; set "
                       "SDN_TOPOLOGY_PYTHON or --topology-python. " +
                       ("; ".join(errors) if errors else "no Python candidates found"))


def validate_controller_interpreter(configured=None, repo_root=PROJECT_ROOT):
    """Verify the controller's project Python 3.10 and Ryu imports."""
    configured = configured or os.environ.get("SDN_CONTROLLER_PYTHON")
    interpreter = configured or os.path.join(repo_root, ".venv", "bin", "python")
    prelude = (
        "import os; os.environ.setdefault('EVENTLET_NO_GREENDNS','yes'); "
        "import eventlet.wsgi; "
        "eventlet.wsgi.ALREADY_HANDLED = getattr(eventlet.wsgi,'ALREADY_HANDLED',object())")
    return verify_python_imports(
        interpreter, ("ryu", "eventlet.wsgi", "ryu.cmd.manager", "ryu.ofproto.ofproto_v1_3"),
        "Ryu controller", required_version=(3, 10), prelude=prelude)


def topology_launch_command(interpreter, topo_script, environment, euid=None):
    """Build a sudo-safe topology command with explicit run-scoped environment."""
    euid = os.geteuid() if euid is None else euid
    if euid == 0 or os.name == "nt":
        return [interpreter, topo_script]
    names = ("PYTHONUNBUFFERED", "SDN_RUNTIME_STATE_FILE", "SDN_RUN_TOKEN",
             "SDN_FAULT_LOG", "SDN_CLEANUP_LOG")
    assignments = ["%s=%s" % (name, environment[name]) for name in names
                   if name in environment]
    return ["sudo", "-E", "--", shutil.which("env") or "/usr/bin/env"] + \
        assignments + [interpreter, topo_script]


def verify_source_copies(topo_script, controller_script=DEFAULT_CONTROLLER_SCRIPT):
    topo_root = os.path.dirname(os.path.dirname(os.path.realpath(topo_script)))
    source_pairs = [
        (topo_script, DEFAULT_TOPO_SCRIPT),
        (controller_script, DEFAULT_CONTROLLER_SCRIPT),
        (os.path.join(topo_root, "faults", "faults.py"),
         os.path.join(PROJECT_ROOT, "faults", "faults.py")),
        (os.path.join(topo_root, "agents", "agent.py"),
         os.path.join(PROJECT_ROOT, "agents", "agent.py")),
    ]
    for candidate, canonical in source_pairs:
        if file_sha256(candidate) != file_sha256(canonical):
            raise RuntimeError("evaluation copy differs from repository source: %s" % candidate)


def summarize_measurements(samples):
    """Average independently captured values by phase; absent data stays null."""
    output = {}
    for phase in ("baseline", "fault", "post_clear"):
        rows = [sample for sample in samples if sample.get("phase") == phase]
        output[phase] = {}
        for field in ("rtt_ms", "packet_loss_pct", "host_interface_aggregate_mbps"):
            values = [row[field] for row in rows if isinstance(row.get(field), (int, float))]
            output[phase][field] = round(sum(values) / len(values), 4) if values else None
    return output


def find_service_recovery(samples, fault_ts, baseline_samples, cleanup_ts=None):
    """Return time to two healthy probes after verified cleanup."""
    baseline = [row["rtt_ms"] for row in baseline_samples
                if row.get("rtt_ms") is not None and row.get("packet_loss_pct") == 0]
    if not baseline or fault_ts is None or cleanup_ts is None:
        return None
    baseline.sort()
    median = baseline[len(baseline) // 2]
    threshold = max(median * 2.0, median + 20.0)
    streak = 0
    for row in samples:
        if row.get("phase") != "post_clear" or row.get("timestamp", 0) < cleanup_ts:
            continue
        healthy = (row.get("rtt_ms") is not None and row.get("packet_loss_pct") == 0
                   and row["rtt_ms"] <= threshold)
        streak = streak + 1 if healthy else 0
        if streak >= RECOVERY_CONSECUTIVE_SAMPLES:
            return round(row["timestamp"] - fault_ts, 3)
    return None


def verify_controller_log(controller_log, requested_mode, expected_script=None,
                          expected_interpreter=None):
    """Verify listener identity, script-derived log path, process freshness and mode."""
    try:
        output = subprocess.check_output(["ss", "-ltnp", "sport", "=", ":6653"],
                                         text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("cannot identify the OpenFlow listener: %s" % exc)
    pid_match = re.search(r"pid=(\d+)", output)
    if not pid_match:
        raise RuntimeError("port 6653 listener PID is unavailable")
    pid = int(pid_match.group(1))
    proc_root = "/proc/%d" % pid
    try:
        executable = os.path.realpath(os.readlink(proc_root + "/exe"))
        with open(proc_root + "/cmdline", "rb") as command_line:
            argv = [arg.decode() for arg in command_line.read().split(b"\0") if arg]
        cwd = os.path.realpath(os.readlink(proc_root + "/cwd"))
        with open(proc_root + "/stat", encoding="utf-8") as process_stat:
            stat_tail = process_stat.read().rsplit(")", 1)[1].split()
        with open("/proc/stat", encoding="utf-8") as system_stat:
            boot_time = float(next(line.split()[1] for line in system_stat
                                   if line.startswith("btime ")))
        start_epoch = boot_time + int(stat_tail[19]) / float(os.sysconf("SC_CLK_TCK"))
    except (OSError, StopIteration, ValueError, IndexError) as exc:
        raise RuntimeError("cannot verify controller process %d: %s" % (pid, exc))
    if expected_interpreter:
        verify_interpreter_match(executable, expected_interpreter,
                                 "controller listener PID %d" % pid)
    argv_paths = [os.path.realpath(arg if os.path.isabs(arg) else os.path.join(cwd, arg))
                  for arg in argv if arg.endswith(".py")]
    if expected_script:
        script_real = os.path.realpath(expected_script if os.path.isabs(expected_script)
                                       else os.path.join(cwd, expected_script))
        if script_real not in argv_paths:
            raise RuntimeError("listener PID %d does not load %s" % (pid, script_real))
    else:
        candidates = [path for path in argv_paths if os.path.basename(path) == "sdn_controller.py"]
        if len(candidates) != 1:
            raise RuntimeError("listener PID %d does not identify exactly one sdn_controller.py" % pid)
        script_real = candidates[0]
    if file_sha256(script_real) != file_sha256(DEFAULT_CONTROLLER_SCRIPT):
        raise RuntimeError("active controller source differs from verified repository source")
    if os.path.getmtime(script_real) > start_epoch + 0.25:
        raise RuntimeError("controller source changed after listener PID %d started" % pid)
    expected_log = os.path.join(os.path.dirname(os.path.dirname(script_real)), "logs", "controller.log")
    if os.path.realpath(controller_log) != os.path.realpath(expected_log):
        raise RuntimeError("selected controller log does not match listener script log path")
    startup = []
    for line in read_file_tail(controller_log, max_lines=5000):
        if "SDN Controller initialized. Mode:" in line:
            epoch = re.search(r"\[([0-9]{10}(?:\.\d+)?)\]", line)
            if epoch:
                startup.append((float(epoch.group(1)), line))
    validate_startup_marker(startup, requested_mode, start_epoch)
    return {"pid": pid, "script": script_real, "log": os.path.realpath(controller_log),
            "interpreter": executable,
            "started": start_epoch}


def validate_startup_marker(startup_markers, requested_mode, process_start):
    if not startup_markers or startup_markers[-1][0] < process_start - 0.25:
        raise RuntimeError("controller log lacks a fresh startup marker")
    mode_match = re.search(r"Mode:\s*\[(BASELINE|DYNAMIC)\]", startup_markers[-1][1], re.I)
    actual = mode_match.group(1).lower() if mode_match else None
    if actual != requested_mode:
        raise RuntimeError("controller startup mode %r does not match requested %s" %
                           (actual, requested_mode))


def verify_interpreter_match(actual, expected, process_label="process"):
    """Fail closed when a live process uses a different Python executable."""
    actual_real = os.path.realpath(actual)
    expected_real = os.path.realpath(expected)
    if actual_real != expected_real:
        raise RuntimeError("%s uses %s; expected %s" %
                           (process_label, actual_real, expected_real))
    return actual_real


def terminate_owned_process_group(proc, process_group_id):
    """Terminate only the process group created for this runner's topology."""
    try:
        os.killpg(process_group_id, signal.SIGINT)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                return False
    return proc.poll() is not None


def signal_verified_host_pid(pid, sig):
    """Signal one previously identity-verified host PID, using sudo if needed."""
    if os.geteuid() == 0:
        os.kill(pid, sig)
        return
    signal_name = signal.Signals(sig).name
    result = subprocess.run(["sudo", "kill", "-%s" % signal_name, str(pid)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=4, check=False)
    if result.returncode:
        raise OSError(result.stderr.strip() or "sudo kill failed")


class MininetSubprocessRunner:
    """Run Mininet commands serially and clean run-owned state out of band."""

    def __init__(self, topo_script, repo_root, outdir=None, fault_log_path=None,
                 cleanup_log_path=None, topology_python=None):
        self.topo_script = topo_script
        self.repo_root = repo_root
        self.outdir = os.path.abspath(outdir or DEFAULT_LOGS_DIR)
        self.fault_log_path = os.path.abspath(fault_log_path or DEFAULT_FAULT_LOG)
        self.cleanup_log_path = os.path.abspath(
            cleanup_log_path or os.path.join(self.outdir, "cleanup_events.log"))
        self.cleanup_start_pos = (os.path.getsize(self.cleanup_log_path)
                                  if os.path.exists(self.cleanup_log_path) else 0)
        self.proc = None
        self.stdout_lines = []
        self._reader_thread = None
        self._stop_reader = False
        self.process_group_id = None
        self.run_token = uuid.uuid4().hex
        self.runtime_state_path = os.path.join(
            self.outdir, ".runtime_%d_%s.json" % (os.getpid(), self.run_token))
        self._condition = threading.Condition()
        self._command_lock = threading.Lock()
        self.command_channel_healthy = True
        self.cleanup_errors = []
        self.measure_timeout_sec = 8.0
        self.topology_python = topology_python

    def _reader(self):
        while not self._stop_reader and self.proc and self.proc.stdout:
            try:
                line = self.proc.stdout.readline()
                if not line:
                    break
                with self._condition:
                    self.stdout_lines.append(line.rstrip())
                    self._condition.notify_all()
            except Exception:
                break

    def start(self):
        self.topology_python = self.topology_python or select_topology_interpreter()

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        os.makedirs(self.outdir, exist_ok=True)
        env["SDN_RUNTIME_STATE_FILE"] = self.runtime_state_path
        env["SDN_RUN_TOKEN"] = self.run_token
        env["SDN_FAULT_LOG"] = self.fault_log_path
        env["SDN_CLEANUP_LOG"] = self.cleanup_log_path
        cmd = topology_launch_command(self.topology_python, self.topo_script, env)
        log_runner(f"Starting Mininet process: {' '.join(cmd)}")

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=self.repo_root,
            env=env,
            start_new_session=True,
        )
        self.process_group_id = self.proc.pid

        self._stop_reader = False
        self._reader_thread = threading.Thread(target=self._reader, daemon=True)
        self._reader_thread.start()

    def send_command(self, cmd_str, wait_sec=1.0):
        if not self.proc or self.proc.poll() is not None:
            log_runner(f"Warning: Mininet process is not running, cannot send '{cmd_str}'")
            return False
        if cmd_str.strip() in ("exit", "quit"):
            return self._send_exit()
        return self._send_acknowledged(cmd_str, max(1.0, wait_sec))

    def _send_acknowledged(self, cmd_str, timeout):
        if not self.command_channel_healthy:
            return False
        with self._command_lock:
            token = uuid.uuid4().hex
            with self._condition:
                start = len(self.stdout_lines)
            log_runner("Mininet CLI << %s" % cmd_str)
            try:
                self.proc.stdin.write(cmd_str + "\nrunner_ack " + token + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                log_runner("Error writing to Mininet stdin: %s" % exc)
                self.command_channel_healthy = False
                return False
            deadline = time.monotonic() + timeout
            with self._condition:
                while time.monotonic() < deadline:
                    try:
                        acknowledged = validate_runner_acknowledgments(
                            self.stdout_lines[start:], token)
                    except ValueError as exc:
                        self.command_channel_healthy = False
                        log_runner("Invalid Mininet CLI acknowledgment: %s" % exc)
                        return False
                    if acknowledged:
                        return True
                    if not self.proc or self.proc.poll() is not None:
                        self.command_channel_healthy = False
                        return False
                    self._condition.wait(min(0.1, deadline - time.monotonic()))
            self.command_channel_healthy = False
            log_runner("Mininet CLI command did not acknowledge within %.1fs: %s" %
                       (timeout, cmd_str))
            return False

    def _send_exit(self):
        try:
            self.proc.stdin.write("exit\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def measure(self, host, peer):
        """Request one independent namespace ping/counter sample from Mininet."""
        if not self.proc or self.proc.poll() is not None:
            return None
        with self._command_lock:
            token = uuid.uuid4().hex
            with self._condition:
                start = len(self.stdout_lines)
            try:
                self.proc.stdin.write("measure %s %s\nrunner_ack %s\n" %
                                      (host, peer, token))
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.command_channel_healthy = False
                return None
            deadline = time.monotonic() + self.measure_timeout_sec
            measurement = None
            measurement_seen = False
            got_ack = False
            with self._condition:
                while time.monotonic() < deadline:
                    for line in self.stdout_lines[start:]:
                        if "MEASURE_JSON=" in line:
                            measurement_seen = True
                            try:
                                measurement = json.loads(line.split("MEASURE_JSON=", 1)[1])
                            except (ValueError, TypeError):
                                measurement = None
                        elif "MEASURE_ERROR=" in line:
                            measurement_seen = True
                            measurement = None
                    try:
                        got_ack = validate_runner_acknowledgments(
                            self.stdout_lines[start:], token)
                    except ValueError as exc:
                        self.command_channel_healthy = False
                        log_runner("Invalid Mininet measurement acknowledgment: %s" % exc)
                        return None
                    if got_ack and measurement_seen:
                        return measurement
                    if not self.proc or self.proc.poll() is not None:
                        self.command_channel_healthy = False
                        return None
                    self._condition.wait(min(0.1, deadline - time.monotonic()))
            self.command_channel_healthy = False
            log_runner("Measurement CLI command timed out: %s -> %s" % (host, peer))
            return None

    def pingall(self):
        """Run Mininet's full reachability matrix and require all 42 replies."""
        with self._condition:
            start = len(self.stdout_lines)
        if not self.send_command("pingall", wait_sec=60):
            return False
        lines = self.stdout_lines[start:]
        for line in lines:
            match = re.search(r"\*\*\* Results:\s*(\d+(?:\.\d+)?)% dropped\s*"
                              r"\((\d+)/(\d+) received\)", line)
            if match:
                return float(match.group(1)) == 0.0 and match.group(2, 3) == ("42", "42")
        return False

    def agents_running(self):
        """Verify six exact host-visible agent PIDs against run and host identity."""
        state = self._load_manifest()
        if not state or state.get("run_token") != self.run_token:
            return False
        agents = state.get("agents", {})
        if set(agents) != {"h%d" % number for number in range(1, 7)}:
            return False
        host_pids = state.get("host_pids", {})
        for host, record in agents.items():
            if not isinstance(record, dict):
                return False
            pid = record.get("pid")
            expected_host_pid = host_pids.get(host)
            if not isinstance(pid, int) or not isinstance(expected_host_pid, int):
                return False
            details = read_host_process_identity(pid)
            if not agent_record_matches(record, host, self.run_token,
                                        expected_host_pid, details):
                return False
        return True

    @staticmethod
    def _append_cleanup(path, event, timestamp=None):
        stamp = time.time() if timestamp is None else timestamp
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("%.3f %s\n" % (stamp, event))
            f.flush()
            os.fsync(f.fileno())
        return stamp

    def _load_manifest(self):
        try:
            with open(self.runtime_state_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _save_manifest(self, state):
        temp_path = self.runtime_state_path + ".tmp.%d" % os.getpid()
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, self.runtime_state_path)

    def _owned_host_pid(self, pid, host_name):
        if not isinstance(pid, int) or pid <= 0 or not host_name:
            return False
        try:
            if os.getpgid(pid) != self.process_group_id:
                return False
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                command = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            return "mininet:%s" % host_name in command
        except OSError:
            return False

    def _terminate_manifest_process(self, record, expected):
        pid = record.get("pid") if isinstance(record, dict) else None
        if not isinstance(pid, int) or pid <= 0:
            return True
        try:
            if os.getpgid(pid) != self.process_group_id:
                return True
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                command = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            if expected and expected not in command:
                return True
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    with open("/proc/%d/stat" % pid, encoding="utf-8") as f:
                        proc_state = f.read().rsplit(")", 1)[1].split()[0]
                    if proc_state == "Z":
                        return True
                except OSError:
                    return True
                time.sleep(0.02)
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                command = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            if os.getpgid(pid) == self.process_group_id and (not expected or expected in command):
                os.kill(pid, signal.SIGKILL)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    with open("/proc/%d/stat" % pid, encoding="utf-8") as f:
                        if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                            return True
                except OSError:
                    return True
                time.sleep(0.02)
            return False
        except ProcessLookupError:
            return True
        except OSError:
            return False

    def _terminate_agent_record(self, host, record, state):
        """Terminate only an agent whose current PID still matches this run."""
        pid = record.get("pid") if isinstance(record, dict) else None
        if not isinstance(pid, int) or pid <= 0:
            return False
        details = read_host_process_identity(pid)
        if details is None:
            return True  # already exited
        if not agent_record_matches(record, host, self.run_token,
                                    state.get("host_pids", {}).get(host), details):
            return False
        try:
            signal_verified_host_pid(pid, signal.SIGTERM)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                current = read_host_process_identity(pid)
                if (current is None or current.get("state") == "Z" or
                        current.get("start_ticks") != record.get("start_ticks")):
                    return True
                time.sleep(0.02)
            current = read_host_process_identity(pid)
            if not agent_record_matches(record, host, self.run_token,
                                        state.get("host_pids", {}).get(host), current):
                return True
            signal_verified_host_pid(pid, signal.SIGKILL)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                current = read_host_process_identity(pid)
                if (current is None or current.get("state") == "Z" or
                        current.get("start_ticks") != record.get("start_ticks")):
                    return True
                time.sleep(0.02)
            return False
        except ProcessLookupError:
            return True
        except OSError:
            return False

    def emergency_cleanup(self):
        """Clear only qdiscs and child processes recorded for this run."""
        state = self._load_manifest()
        if state is None:
            return not os.path.exists(self.runtime_state_path)
        if (state.get("run_token") != self.run_token or
                state.get("process_group_id") != self.process_group_id):
            self.cleanup_errors.append("runtime manifest identity mismatch")
            return False
        host_pids = state.get("host_pids", {})
        for item in list(state.get("active_qdiscs", [])):
            host_name, iface = item.get("host"), item.get("interface")
            pid = item.get("host_pid") or host_pids.get(host_name)
            if not self._owned_host_pid(pid, host_name):
                continue
            prefix = [] if os.geteuid() == 0 else ["sudo"]
            try:
                subprocess.run(prefix + ["mnexec", "-a", str(pid), "tc", "qdisc",
                                         "del", "dev", iface, "root"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, timeout=2, check=False)
                check = subprocess.run(prefix + ["mnexec", "-a", str(pid), "tc",
                                                  "qdisc", "show", "dev", iface],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, timeout=2, check=False)
                if check.returncode or "netem" in check.stdout.lower():
                    self.cleanup_errors.append("could not verify qdisc removal on %s" % iface)
                    continue
                removed_at = time.time()
                self._append_cleanup(self.cleanup_log_path,
                    "QDISC_REMOVED host=%s interface=%s method=out_of_band" %
                    (host_name, iface), removed_at)
                state["active_qdiscs"] = [entry for entry in state.get("active_qdiscs", [])
                                           if entry != item]
                self._save_manifest(state)
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.cleanup_errors.append("qdisc cleanup failed on %s: %s" % (iface, exc))
        for host, record in state.get("agents", {}).items():
            if not self._terminate_agent_record(host, record, state):
                self.cleanup_errors.append("agent cleanup could not be verified on %s" % host)
        for record in state.get("iperf", []):
            if not self._terminate_manifest_process(record, "iperf"):
                self.cleanup_errors.append("iperf cleanup could not be verified")
        return not self.cleanup_errors

    def _finalize_namespace_cleanup(self):
        """After the owned topology exits, verify remaining namespace resources are gone."""
        state = self._load_manifest()
        if not state:
            return False
        for item in list(state.get("active_qdiscs", [])):
            pid = item.get("host_pid") or state.get("host_pids", {}).get(item.get("host"))
            namespace_gone = True
            if isinstance(pid, int):
                try:
                    with open("/proc/%d/stat" % pid, encoding="utf-8") as f:
                        proc_state = f.read().rsplit(")", 1)[1].split()[0]
                    namespace_gone = proc_state == "Z" or os.getpgid(pid) != self.process_group_id
                except OSError:
                    namespace_gone = True
            if not namespace_gone:
                self.cleanup_errors.append("host namespace remains for %s" % item.get("host"))
                continue
            self.cleanup_errors = [error for error in self.cleanup_errors
                                   if error != "could not verify qdisc removal on %s" %
                                   item.get("interface")]
            removed_at = time.time()
            self._append_cleanup(self.cleanup_log_path,
                "QDISC_REMOVED host=%s interface=%s method=namespace_teardown" %
                (item.get("host"), item.get("interface")), removed_at)
            state["active_qdiscs"].remove(item)
            self._save_manifest(state)
        if not state.get("active_qdiscs"):
            state["agents"] = {}
            state["iperf"] = []
            state["downed_links"] = []
            self._save_manifest(state)
        return not state.get("active_qdiscs") and not self.cleanup_errors

    def stop(self):
        cleanup_confirmed = True
        if self.proc:
            try:
                if self.proc.poll() is None:
                    if self.command_channel_healthy:
                        self._send_exit()
                        self.proc.wait(timeout=20)
                    else:
                        raise subprocess.TimeoutExpired("Mininet CLI", 0)
            except subprocess.TimeoutExpired:
                log_runner("Mininet did not exit gracefully; terminating its process group...")
                self.emergency_cleanup()
                process_group_stopped = terminate_owned_process_group(
                    self.proc, self.process_group_id)
                cleanup_confirmed = process_group_stopped
            except Exception as e:
                log_runner(f"Error stopping Mininet process: {e}")
                cleanup_confirmed = False

            if self.proc.poll() is None:
                cleanup_confirmed = False
            if cleanup_confirmed and self.process_group_id is not None:
                try:
                    os.killpg(self.process_group_id, 0)
                except ProcessLookupError:
                    pass
                else:
                    try:
                        os.killpg(self.process_group_id, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    deadline = time.time() + 3
                    while time.time() < deadline:
                        try:
                            os.killpg(self.process_group_id, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.05)
                    else:
                        try:
                            os.killpg(self.process_group_id, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        time.sleep(0.1)
                    try:
                        os.killpg(self.process_group_id, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        cleanup_confirmed = False

        self._stop_reader = True
        if self._reader_thread:
            self._reader_thread.join(timeout=1)
        if "MININET_NET_STOPPED" not in self.stdout_lines:
            cleanup_confirmed = False
        if cleanup_confirmed:
            cleanup_confirmed = self._finalize_namespace_cleanup()
            if cleanup_confirmed:
                lines, _ = read_new_lines_from_file(self.cleanup_log_path,
                                                    self.cleanup_start_pos)
                if not any(line.rstrip().endswith("FAULT_CLEARED") for line in lines):
                    self._append_cleanup(self.cleanup_log_path, "FAULT_CLEARED")
        if not cleanup_confirmed:
            reason = "; ".join(self.cleanup_errors) or "topology cleanup verification failed"
            self._append_cleanup(self.cleanup_log_path, "CLEANUP_FAILED %s" % reason)
        self.proc = None

        # The topology script calls net.stop() in a finally block. Never use
        # global `mn -c`: timeout handling is scoped to this run's
        # process group, whose members inherited the runner-created session.
        return cleanup_confirmed


def simulate_dry_run(scenario, mode, outdir, fault_log_path, controller_log_path):
    """Return explicit dry-run metadata without writing to live evidence logs."""
    log_runner(f"[DRY-RUN] Simulating scenario: {scenario['name']} (mode: {mode})")
    return {
        "fault_timestamp": None,
        "detection_timestamp": None,
        "detection_time": None,
        "detected": None,
        "actual_detection": "DRY-RUN ONLY - no network detection was performed",
        "recovery_time": None,
        "stdout_tail": ["[DRY-RUN] No Mininet/controller test ran; evidence logs were not modified"],
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
    outdir=DEFAULT_LOGS_DIR,
    controller_identity=None,
    topology_python=None,
    controller_python=None,
):
    """Executes one fault scenario under the given mode and collects metrics."""
    outdir = os.path.abspath(outdir)
    fault_log_path = os.path.abspath(fault_log_path)
    controller_log_path = os.path.abspath(controller_log_path)
    run_start_time = time.time()
    run_timestamp_iso = datetime.datetime.now().isoformat()
    execution_error = None
    fault_ts = detect_ts = detect_dt = action_time = recovery_time = None
    action_ts = action_text = None
    detected = False
    actual_detection = "No anomaly detected within detection window"
    measurements = []
    cleanup_confirmed = dry_run
    log_runner("=" * 65)
    log_runner(f"SCENARIO: {scenario['name'].upper()} | MODE: {mode.upper()}")
    log_runner(f"Command : {scenario['command']}")
    log_runner(f"Expected: {scenario['expected_detection']}")
    log_runner(f"Started : {run_timestamp_iso}")
    log_runner("=" * 65)

    # Note current file sizes to only parse new lines
    fault_start_pos = os.path.getsize(fault_log_path) if os.path.exists(fault_log_path) else 0
    controller_start_pos = os.path.getsize(controller_log_path) if os.path.exists(controller_log_path) else 0
    os.makedirs(outdir, exist_ok=True)
    cleanup_log_path = os.path.join(outdir, "cleanup_events.log")
    cleanup_start_pos = os.path.getsize(cleanup_log_path) if os.path.exists(cleanup_log_path) else 0
    cleanup_ts = None

    if dry_run:
        sim = simulate_dry_run(scenario, mode, outdir, fault_log_path, controller_log_path)
        actual_detection = sim["actual_detection"]
        mininet_output = sim["stdout_tail"]
        new_fault_lines = []
        new_ctrl_lines = []
    else:
        mn = MininetSubprocessRunner(topo_script, repo_root, outdir,
                                     fault_log_path, cleanup_log_path,
                                     topology_python=topology_python)
        fault_attempted = False
        fault_cleared = False
        clear_confirmed = False
        try:
            verify_source_copies(topo_script)
            if controller_identity:
                current = verify_controller_log(controller_log_path, mode,
                                                expected_interpreter=controller_python)
                if (current["pid"] != controller_identity["pid"] or
                        current["started"] != controller_identity["started"]):
                    raise RuntimeError("controller process changed after preflight")
            mn.start()
            log_runner("Waiting 4 seconds for topology & NAT initialization...")
            time.sleep(4)

            if not mn.proc or mn.proc.poll() is not None:
                raise RuntimeError("Mininet topology process exited before entering its CLI")
            if controller_identity:
                current = verify_controller_log(controller_log_path, mode,
                                                expected_interpreter=controller_python)
                if (current["pid"] != controller_identity["pid"] or
                        current["started"] != controller_identity["started"]):
                    raise RuntimeError("controller process changed during topology startup")

            startup_lines, _ = read_new_lines_from_file(controller_log_path, controller_start_pos)
            connected_switches = {
                int(dpid)
                for line in startup_lines
                for dpid in re.findall(r"Switch connected:\s*s([1-3])\b", line)
            }
            if connected_switches != {1, 2, 3}:
                missing = sorted({1, 2, 3} - connected_switches)
                raise RuntimeError(
                    "Ryu did not report OpenFlow connections from all expected switches; "
                    f"missing DPID(s): {missing}"
                )
            log_runner("Verified Ryu switch connections: s1, s2, s3.")

            if not mn.pingall():
                raise RuntimeError("pingall did not verify 42/42 connectivity")
            log_runner("Verified pingall: 42/42 replies, 0% loss.")

            # Start host telemetry agents
            log_runner("Starting host telemetry agents...")
            if not mn.send_command("agents start", wait_sec=10):
                raise TimeoutError("agents start did not complete in Mininet CLI")
            if not mn.agents_running():
                raise RuntimeError("agent startup did not verify all six run-owned PIDs")
            log_runner("Verified six telemetry agent processes from the run manifest.")

            host, peer = scenario["probe"]
            baseline_samples = []
            for _ in range(3):
                sample = mn.measure(host, peer)
                if not mn.command_channel_healthy:
                    raise TimeoutError("baseline measurement did not complete in Mininet CLI")
                if sample:
                    sample["phase"] = "baseline"
                    measurements.append(sample)
                    if sample.get("rtt_ms") is not None and sample.get("packet_loss_pct") is not None:
                        baseline_samples.append(sample)

            if len(baseline_samples) != 3 or any(
                    row.get("packet_loss_pct") != 0 or row.get("rtt_ms") is None
                    for row in baseline_samples):
                raise RuntimeError("three healthy baseline ICMP samples were not established")

            # Injection time is taken only from the verified fault event, never
            # from the command-send time or a later cleanup record.
            fault_attempted = True
            fault_start = os.path.getsize(fault_log_path) if os.path.exists(fault_log_path) else 0

            # Inject fault
            log_runner(f"Injecting fault: '{scenario['command']}'")
            if not mn.send_command(scenario["command"], wait_sec=10):
                raise TimeoutError("fault command did not complete in Mininet CLI")
            fault_lines, _ = read_new_lines_from_file(fault_log_path, fault_start)
            fault_ts, _ = extract_fault_timestamp(fault_lines, scenario["command"])

            # Wait for detection window
            log_runner(f"Waiting for detection window ({detection_window}s)...")
            detect_deadline = time.monotonic() + detection_window
            while time.monotonic() < detect_deadline:
                sample = mn.measure(host, peer)
                if not mn.command_channel_healthy:
                    raise TimeoutError("fault-period measurement did not complete in Mininet CLI")
                if sample:
                    sample["phase"] = "fault"
                    measurements.append(sample)
                ctrl_lines, _ = read_new_lines_from_file(controller_log_path, controller_start_pos)
                det, dt, _, _ = parse_controller_anomaly(ctrl_lines, fault_ts, scenario)
                if det and not detected:
                    log_runner(f"Anomaly observed in controller log! Time-to-detect ~ {dt:.2f}s")
                    detected, detect_ts, detect_dt, actual_detection = True, det[0], dt, det[1]
                time.sleep(max(0.0, MEASUREMENT_INTERVAL_SEC - 0.05))

            if controller_identity:
                current = verify_controller_log(controller_log_path, mode,
                                                expected_interpreter=controller_python)
                if (current["pid"] != controller_identity["pid"] or
                        current["started"] != controller_identity["started"]):
                    raise RuntimeError("controller process changed during the scenario")

            # Clear fault
            log_runner("Clearing fault condition ('fault clear')...")
            clear_start = (os.path.getsize(cleanup_log_path)
                           if os.path.exists(cleanup_log_path) else 0)
            if not mn.send_command("fault clear", wait_sec=20):
                raise TimeoutError("fault clear did not complete in Mininet CLI")
            clear_lines, _ = read_new_lines_from_file(cleanup_log_path, clear_start)
            clear_confirmed = verified_clear_event(clear_lines)
            fault_cleared = clear_confirmed
            if not clear_confirmed:
                raise RuntimeError("fault clear was not independently verified")
            cleanup_ts = extract_cleanup_timestamp(clear_lines)

            # Recovery is based solely on independent ping health. Require 2
            # consecutive healthy one-second samples relative to pre-fault RTT.
            recovery_deadline = time.monotonic() + RECOVERY_WINDOW_SEC
            post_clear_count = 0
            while time.monotonic() < recovery_deadline:
                sample = mn.measure(host, peer)
                if not mn.command_channel_healthy:
                    raise TimeoutError("recovery measurement did not complete in Mininet CLI")
                if sample:
                    sample["phase"] = "post_clear"
                    measurements.append(sample)
                    post_clear_count += 1
                    recovery_time = find_service_recovery(
                        measurements, fault_ts, baseline_samples, cleanup_ts)
                    if recovery_time is not None and post_clear_count >= 2:
                        break
                time.sleep(max(0.0, MEASUREMENT_INTERVAL_SEC - 0.05))

            # Stop agents and exit
            log_runner("Stopping agents...")
            if not mn.send_command("agents stop", wait_sec=10):
                raise TimeoutError("agents stop did not complete in Mininet CLI")

        except Exception as exc:
            execution_error = f"{type(exc).__name__}: {exc}"
            log_runner(f"Live scenario setup/execution failed: {execution_error}")

        finally:
            if fault_attempted and not fault_cleared and mn.proc and mn.proc.poll() is None:
                if mn.command_channel_healthy:
                    log_runner("Clearing scenario state after incomplete execution...")
                    clear_start = (os.path.getsize(cleanup_log_path)
                                   if os.path.exists(cleanup_log_path) else 0)
                    if mn.send_command("fault clear", wait_sec=20):
                        clear_lines, _ = read_new_lines_from_file(cleanup_log_path, clear_start)
                        clear_confirmed = verified_clear_event(clear_lines)
                        if clear_confirmed:
                            cleanup_ts = extract_cleanup_timestamp(clear_lines)
                if not clear_confirmed:
                    log_runner("Interactive cleanup unavailable; using manifest-scoped cleanup.")
                    mn.emergency_cleanup()
            log_runner("Stopping Mininet...")
            cleanup_confirmed = mn.stop()
            cleanup_lines, _ = read_new_lines_from_file(cleanup_log_path, cleanup_start_pos)
            if verified_clear_event(cleanup_lines):
                clear_confirmed = True
                fault_cleared = True
                try:
                    cleanup_ts = extract_cleanup_timestamp(cleanup_lines)
                except ValueError:
                    cleanup_ts = None
            mininet_output = mn.stdout_lines
            if not cleanup_confirmed:
                execution_error = execution_error or "runner-owned Mininet process group cleanup unconfirmed"

        # Collect new log lines
        new_fault_lines, _ = read_new_lines_from_file(fault_log_path, fault_start_pos)
        new_ctrl_lines, _ = read_new_lines_from_file(controller_log_path, controller_start_pos)
        new_cleanup_lines, _ = read_new_lines_from_file(cleanup_log_path, cleanup_start_pos)

        # Re-parse the append-only fault log and reject missing/duplicate events.
        if fault_attempted:
            try:
                fault_ts, _ = extract_fault_timestamp(new_fault_lines, scenario["command"])
            except ValueError as exc:
                fault_ts = None
                execution_error = execution_error or str(exc)

        # Parse detection from controller log
        det_entry, detect_dt, action_ts, action_text = parse_controller_anomaly(new_ctrl_lines, fault_ts, scenario)
        if det_entry:
            detected = True
            detect_ts = det_entry[0]
            actual_detection = det_entry[1]
        else:
            detected = False
            detect_ts = None
            detect_dt = None
            if execution_error:
                actual_detection = f"Not measured; setup/execution failed: {execution_error}"
            else:
                actual_detection = "No anomaly detected within detection window"

        # Action latency is relative to the attributed detection event.
        action_time = round(action_ts - detect_ts, 3) if action_ts is not None and detect_ts else None

    run_end_time = time.time()
    log_runner(f"Result: Detected={detected} | Time-to-detect={detect_dt}s | Actual='{actual_detection}'")

    result = {
        "dry_run": dry_run,
        "scenario": scenario["id"],
        "scenario_name": scenario["name"],
        "mode": mode,
        "command": scenario["command"],
        "expected_detection": scenario["expected_detection"],
        "actual_detection": actual_detection,
        "detected": detected,
        "execution_error": execution_error,
        "scenario_complete": bool(not dry_run and execution_error is None and cleanup_confirmed
                                   and clear_confirmed and fault_ts is not None),
        "time_to_detect_sec": detect_dt,
        "time_to_action_sec": action_time if not dry_run else None,
        "controller_action_attributed": action_text,
        "time_to_recover_sec": recovery_time,
        "measurements": measurements,
        "measurement_summary": summarize_measurements(measurements),
        "measurement_method": {
            "interval_sec": MEASUREMENT_INTERVAL_SEC,
            "rtt_ms": "one ICMP echo round trip per sample from Mininet host namespace",
            "packet_loss_pct": "0 or 100 percent for each one-echo sample; not a bulk-flow estimate",
            "throughput_mbps": "aggregate RX+TX host-interface byte-counter delta per interval",
            "throughput_limitations": "host-interface aggregate, includes unrelated host traffic and probe overhead",
            "recovery_threshold": ("after a verified cleanup timestamp, zero echo loss and RTT <= max(2x "
                                   "baseline median, baseline median + 20 ms) for two consecutive samples; "
                                   "null without valid baseline RTT or cleanup timestamp"),
            "service_health_scope": "ICMP reachability and RTT for the scenario probe pair, not telemetry-agent recovery",
            "recovery_window_sec": RECOVERY_WINDOW_SEC,
        },
        "timestamps": {
            "run_started": run_timestamp_iso,
            "fault_injected": fault_ts,
            "detected": detect_ts,
            "action": action_ts,
            "cleanup_verified": cleanup_ts,
            "run_ended": datetime.datetime.now().isoformat(),
            "duration_sec": round(run_end_time - run_start_time, 2),
            "cleanup_confirmed": cleanup_confirmed,
        },
        "logs": {
            "fault_events_tail": [] if dry_run else read_file_tail(fault_log_path, max_lines=20),
            "cleanup_events_tail": [] if dry_run else read_file_tail(cleanup_log_path, max_lines=20),
            "controller_log_tail": [] if dry_run else read_file_tail(controller_log_path, max_lines=30),
            "mininet_stdout_tail": mininet_output[-25:] if mininet_output else [],
        },
    }

    raw_stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    os.makedirs(outdir, exist_ok=True)
    raw_path = os.path.join(outdir, f"raw_{scenario['id']}_{mode}_{raw_stamp}.log")
    with open(raw_path, "x", encoding="utf-8") as raw:
        if dry_run:
            raw.write("DRY-RUN ONLY: no Mininet, controller, or fault evidence was collected.\n")
        else:
            raw.write("=== Fault events generated during this run ===\n")
            raw.write("\n".join(new_fault_lines) + "\n")
            raw.write("=== Controller log entries generated during this run ===\n")
            raw.write("\n".join(new_ctrl_lines) + "\n")
            raw.write("=== Cleanup events generated during this run ===\n")
            raw.write("\n".join(new_cleanup_lines) + "\n")
            raw.write("=== Full Mininet stdout/stderr ===\n")
            raw.write("\n".join(mininet_output) + "\n")
    result["raw_log"] = os.path.basename(raw_path)

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
        status_str = (
            "SIMULATED" if r.get("dry_run")
            else ("INCOMPLETE" if not r.get("scenario_complete")
                  else ("PASS" if r["detected"] else "FAIL"))
        )

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
        "--topology-python",
        default=os.environ.get("SDN_TOPOLOGY_PYTHON"),
        help="Python that imports Mininet (default: auto-detect; env SDN_TOPOLOGY_PYTHON)",
    )
    parser.add_argument(
        "--controller-python",
        default=os.environ.get("SDN_CONTROLLER_PYTHON", DEFAULT_CONTROLLER_PYTHON),
        help="Python 3.10 used by the active Ryu controller (default: project .venv)",
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

    topology_python = controller_python = None
    if not args.dry_run:
        if args.mode == "both":
            parser.error(
                "Live --mode both is unsafe: this runner cannot restart/reconfigure Ryu. "
                "Run one batch at a time and restart Ryu with the matching --mode."
            )
        try:
            verify_source_copies(args.topo)
            topology_python = select_topology_interpreter(args.topology_python)
            controller_python = validate_controller_interpreter(args.controller_python)
            topo_root = os.path.dirname(os.path.dirname(os.path.realpath(args.topo)))
            expected_fault_log = os.path.join(topo_root, "logs", "fault_events.log")
            if os.path.realpath(args.fault_log) != os.path.realpath(expected_fault_log):
                raise RuntimeError("fault log path does not belong to selected topology copy")
            controller_identity = verify_controller_log(
                args.controller_log, args.mode, expected_interpreter=controller_python)
        except (OSError, RuntimeError) as exc:
            parser.error(str(exc))
        log_runner("Verified listener PID %d, controller source, log path and fresh %s startup marker." %
                   (controller_identity["pid"], args.mode.upper()))
        log_runner("Verified topology Python: %s" % topology_python)
        log_runner("Verified controller Python: %s" % controller_python)
    else:
        verify_source_copies(args.topo)
        controller_identity = None

    all_results = []
    mode_outdirs = {}
    for mode in modes:
        run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        mode_outdirs[mode] = mode_run_directory(args.outdir, mode, run_id)

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
                outdir=mode_outdirs[mode],
                controller_identity=controller_identity,
                topology_python=topology_python,
                controller_python=controller_python,
            )
            all_results.append(res)

            # Save individual run file: logs/run_<scenario>_<mode>_<timestamp>.json
            ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            run_filename = f"run_{sc['id']}_{mode}_{ts_str}.json"
            run_filepath = os.path.join(mode_outdirs[mode], run_filename)
            with open(run_filepath, "x", encoding="utf-8") as f:
                json.dump(res, f, indent=2)
            log_runner(f"Saved run record: {run_filepath}")

            time.sleep(1.0)

    # Save a unique summary so previous run evidence is never overwritten.
    summary_stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    summary_dir = mode_outdirs[modes[0]] if len(modes) == 1 else args.outdir
    summary_path = os.path.join(summary_dir, f"results_summary_{summary_stamp}.json")
    summary_data = {
        "generated_at": datetime.datetime.now().isoformat(),
        "total_runs": len(all_results),
        "passed_detections": sum(1 for r in all_results
                                  if r["detected"] and r.get("scenario_complete")),
        "modes_tested": modes,
        "runs": all_results,
    }
    with open(summary_path, "x", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2)
    log_runner(f"Saved results summary: {summary_path}")

    # Print summary table
    print_summary_table(all_results)
    log_runner("Test execution completed successfully.")


if __name__ == "__main__":
    main()
