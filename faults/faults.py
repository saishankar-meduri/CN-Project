"""Fault injection + agent launcher as extra Mininet CLI commands.

Commands (type at the mininet> prompt):
  agents start | agents stop
  fault flood <src> <dst> [secs]     UDP flood with iperf (50 Mbps)
  fault delay <host> <ms>            tc netem delay on host's link
  fault loss <host> <percent>        tc netem loss on host's link
  fault killagent <host>             kill that host's telemetry agent
  fault linkdown <sw1> <sw2>         bring a switch-switch link down
  fault clear                        undo everything above

Every injection is timestamped in logs/fault_events.log (for time-to-detect).
"""
import os
import re
import json
import subprocess
import sys
import time
import uuid
from mininet.cli import CLI

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.join(HERE, "..", "agents", "agent.py")
LOGFILE = os.environ.get("SDN_FAULT_LOG", os.path.join(HERE, "..", "logs", "fault_events.log"))
CLEANUP_LOGFILE = os.environ.get(
    "SDN_CLEANUP_LOG", os.path.join(HERE, "..", "logs", "cleanup_events.log"))
RUNTIME_STATE_FILE = os.environ.get("SDN_RUNTIME_STATE_FILE")
# each host probes a host on a different switch
PEERS = {"h1": "h4", "h2": "h5", "h3": "h6", "h4": "h1", "h5": "h2", "h6": "h3"}


def log_event(text, timestamp=None):
    os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
    line = "%.3f %s" % (timestamp if timestamp is not None else time.time(), text)
    with open(LOGFILE, "a") as f:
        f.write(line + "\n")
    print("[fault] " + line)


def log_cleanup_event(text, timestamp=None, path=None):
    """Append cleanup evidence separately from successful injection events."""
    cleanup_path = path or CLEANUP_LOGFILE
    os.makedirs(os.path.dirname(os.path.abspath(cleanup_path)), exist_ok=True)
    line = "%.3f %s" % (timestamp if timestamp is not None else time.time(), text)
    with open(cleanup_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    print("[cleanup] " + line, flush=True)


def _run_host_argv(host, argv, timeout=3.0, check_return=True):
    """Run one bounded command inside a Mininet node namespace."""
    proc = host.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True)
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                raise FaultInjectionError("command could not be reaped: %s" % argv[0]) from exc
        raise FaultInjectionError("command timed out after %.1fs: %s" %
                                  (timeout, " ".join(argv))) from exc
    if check_return and proc.returncode:
        raise FaultInjectionError("command failed (%d): %s: %s" %
                                  (proc.returncode, " ".join(argv), (output or "").strip()))
    return output or ""


def _write_runtime_state(state):
    path = state.get("manifest_path")
    if not path:
        return
    temp_path = path + ".tmp.%d" % os.getpid()
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, path)


def _read_process_identity(pid):
    """Read host-visible Linux process argv, state, and start time."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            argv = [part.decode("utf-8", "replace")
                    for part in f.read().split(b"\0") if part]
        with open("/proc/%d/stat" % pid, encoding="utf-8") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return {"argv": argv, "state": fields[0], "start_ticks": int(fields[19])}
    except (OSError, ValueError, IndexError):
        return None


def _agent_identity_matches(details, host, run_id, script_path):
    if not details or details.get("state") == "Z":
        return False
    argv = details.get("argv", [])
    script_real = os.path.realpath(script_path)
    script_present = any(arg.endswith(".py") and os.path.realpath(arg) == script_real
                         for arg in argv)
    return (script_present and "--id=%s" % host in argv
            and "--run-id=%s" % run_id in argv)


class FaultInjectionError(RuntimeError):
    pass


def _interface_bytes(host, iface):
    raw = _run_host_argv(host, ["cat", "/sys/class/net/%s/statistics/tx_bytes" % iface])
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        raise FaultInjectionError("could not read transmit counter for %s" % iface)


def inject_fault(net, kind, args, state):
    """Inject and verify one fault. Return its canonical event only on success."""
    runtime_state = state.get("runtime_state", state)
    try:
        if kind == "flood":
            if len(args) not in (2, 3):
                raise FaultInjectionError("usage: fault flood <src> <dst> [secs]")
            src_name, dst_name = args[:2]
            src, dst = net[src_name], net[dst_name]
            secs = int(args[2]) if len(args) == 3 else 20
            if secs < 2:
                raise FaultInjectionError("flood duration must be at least 2 seconds")
            port = 55001
            server = dst.popen(["iperf", "-s", "-u", "-p", str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            state.setdefault("iperf", []).append(server)
            runtime_state.setdefault("iperf", []).append(
                {"pid": server.pid, "command": ["iperf", "-s", "-u", "-p", str(port)]})
            _write_runtime_state(runtime_state)
            time.sleep(0.2)
            if server.poll() is not None:
                raise FaultInjectionError("iperf UDP server exited during startup")
            iface = "%s-eth0" % src_name
            before = _interface_bytes(src, iface)
            client = src.popen(["iperf", "-c", dst.IP(), "-u", "-p", str(port),
                                "-b", "50M", "-t", str(secs)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            state.setdefault("iperf", []).append(client)
            runtime_state.setdefault("iperf", []).append({
                "pid": client.pid,
                "command": ["iperf", "-c", dst.IP(), "-u", "-p", str(port),
                            "-b", "50M", "-t", str(secs)],
            })
            _write_runtime_state(runtime_state)
            activated_at = time.time()
            time.sleep(0.5)
            after = _interface_bytes(src, iface)
            if (client.poll() is not None or server.poll() is not None or
                    after - before < 65536):
                for proc in (client, server):
                    if proc.poll() is None:
                        proc.terminate()
                raise FaultInjectionError("iperf did not remain active and produce TX traffic")
            return "flood %s %s %d" % (src_name, dst_name, secs), activated_at

        if kind in ("delay", "loss"):
            expected_args = 2
            if len(args) != expected_args:
                raise FaultInjectionError("usage: fault %s <host> <%s>" %
                                          (kind, "ms" if kind == "delay" else "percent"))
            host_name, value = args
            host = net[host_name]
            number = float(value)
            if number <= 0 or (kind == "loss" and number > 100):
                raise FaultInjectionError("invalid %s value: %s" % (kind, value))
            iface = "%s-eth0" % host_name
            qdisc = {"host": host_name, "interface": iface,
                     "host_pid": runtime_state.get("host_pids", {}).get(host_name)}
            if qdisc not in runtime_state.setdefault("active_qdiscs", []):
                runtime_state["active_qdiscs"].append(qdisc)
                _write_runtime_state(runtime_state)
            if kind == "delay":
                _run_host_argv(host, ["tc", "qdisc", "replace", "dev", iface,
                                      "root", "netem", "delay", "%sms" % value])
                expected = "delay %sms" % value
            else:
                _run_host_argv(host, ["tc", "qdisc", "replace", "dev", iface,
                                      "root", "netem", "loss", "%s%%" % value])
                expected = "loss %s%%" % value
            observed = _run_host_argv(host, ["tc", "qdisc", "show", "dev", iface]).lower()
            if "netem" not in observed or expected not in observed:
                raise FaultInjectionError("tc qdisc verification failed: %s" % observed.strip())
            activated_at = time.time()
            return "%s %s %s" % (kind, host_name, value), activated_at

        if kind == "killagent":
            if len(args) != 1 or args[0] not in PEERS:
                raise FaultInjectionError("usage: fault killagent <host>")
            host_name = args[0]
            host = net[host_name]
            proc = state.get("agents", {}).get(host_name)
            if proc is None or proc.poll() is not None:
                raise FaultInjectionError("tracked telemetry agent is not running on %s" % host_name)
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
            if proc.poll() is None:
                raise FaultInjectionError("agent process remains on %s" % host_name)
            return "killagent %s" % host_name, time.time()

        if kind == "linkdown":
            if len(args) != 2:
                raise FaultInjectionError("usage: fault linkdown <switch> <switch>")
            sw1, sw2 = args
            links = net.linksBetween(net[sw1], net[sw2])
            if not links:
                raise FaultInjectionError("no link exists between %s and %s" % (sw1, sw2))
            state.setdefault("downed", []).append((sw1, sw2))
            net.configLinkStatus(sw1, sw2, "down")
            activated_at = time.time()
            if any(link.intf1.isUp() or link.intf2.isUp() for link in links):
                raise FaultInjectionError("link state verification failed for %s-%s" % (sw1, sw2))
            runtime_state.setdefault("downed_links", []).append({"switches": [sw1, sw2]})
            _write_runtime_state(runtime_state)
            return "linkdown %s %s" % (sw1, sw2), activated_at

        raise FaultInjectionError("unknown fault kind: %s" % kind)
    except FaultInjectionError:
        raise
    except Exception as exc:
        raise FaultInjectionError("%s injection/verification error: %s" % (kind, exc))


class FaultCLI(CLI):
    def __init__(self, *args, **kwargs):
        # Mininet's CLI.__init__ enters cmdloop before returning. Command
        # handlers can therefore run while this constructor is still inside
        # super().__init__, so initialize every field they use first.
        self.downed = []
        self.active_processes = []
        self.agent_processes = {}
        self.measurement_counters = {}
        self._last_cleanup_success = True
        self._cleanup_event_logged = False
        self.runtime_state = {
            "manifest_path": RUNTIME_STATE_FILE,
            "run_token": os.environ.get("SDN_RUN_TOKEN"),
            "topology_pid": os.getppid(),
            "process_group_id": os.getpgrp(),
            "host_pids": {},
            "switch_pids": {},
            "agents": {},
            "iperf": [],
            "active_qdiscs": [],
            "downed_links": [],
        }
        if RUNTIME_STATE_FILE and os.path.exists(RUNTIME_STATE_FILE):
            try:
                with open(RUNTIME_STATE_FILE, encoding="utf-8") as f:
                    self.runtime_state.update(json.load(f))
            except (OSError, ValueError):
                pass
        if not self.runtime_state.get("run_token"):
            self.runtime_state["run_token"] = uuid.uuid4().hex
        try:
            super().__init__(*args, **kwargs)
        except BaseException as init_error:
            # A command may have started a fault or processes before CLI setup
            # failed. Clear the same run-owned resources before propagating.
            try:
                self._clear_run_state(getattr(self, "mn", None))
                cleanup_errors = []
            except Exception as exc:
                log_cleanup_event("CLEANUP_FAILED during FaultCLI initialization: %s" % exc)
                cleanup_errors = [str(exc)]
            if cleanup_errors:
                raise RuntimeError(
                    "FaultCLI initialization failed and process cleanup was incomplete: "
                    + "; ".join(cleanup_errors)
                ) from init_error
            raise

    def do_measure(self, line):
        """measure <host> <peer>: independent ping and host-interface counter sample."""
        args = line.split()
        try:
            if len(args) != 2 or args[0] not in PEERS or args[1] not in PEERS:
                raise ValueError("usage: measure <host> <peer>")
            host_name, peer_name = args
            host = self.mn[host_name]
            iface = host_name + "-eth0"
            started = time.time()
            ping = _run_host_argv(host, ["ping", "-n", "-c", "1", "-W", "1",
                                         self.mn[peer_name].IP()], timeout=3.0)
            ended = time.time()
            loss = 0.0 if "1 received" in ping or "1 packets received" in ping else 100.0
            rtt = None
            match = re.search(r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = "
                              r"[\d.]+/([\d.]+)/", ping)
            if match:
                rtt = float(match.group(1))
            counters = _run_host_argv(host, ["cat",
                "/sys/class/net/%s/statistics/tx_bytes" % iface,
                "/sys/class/net/%s/statistics/rx_bytes" % iface], timeout=2.0)
            values = [int(value) for value in counters.split()]
            if len(values) != 2:
                raise FaultInjectionError("invalid interface counters for %s" % iface)
            tx, rx = values
            current = (tx, rx, ended)
            previous = self.measurement_counters.get(host_name)
            throughput = None
            sample_interval = None
            if previous and ended > previous[2]:
                sample_interval = ended - previous[2]
                throughput = ((tx - previous[0] + rx - previous[1]) * 8.0 /
                              (ended - previous[2]) / 1_000_000.0)
            self.measurement_counters[host_name] = current
            record = {"host": host_name, "peer": peer_name, "timestamp": ended,
                      "probe_duration_sec": round(ended - started, 3),
                      "counter_interval_sec": round(sample_interval, 3) if sample_interval else None,
                      "rtt_ms": rtt,
                      "packet_loss_pct": loss, "host_interface_aggregate_mbps": throughput}
            print("MEASURE_JSON=" + json.dumps(record, sort_keys=True), flush=True)
        except Exception as exc:
            print("MEASURE_ERROR=" + str(exc), flush=True)

    def do_runner_ack(self, token):
        """Private synchronization command used by the subprocess runner."""
        token = token.strip()
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            print("RUNNER_ACK_ERROR=malformed-token", flush=True)
            return
        # Mininet's cmd.Cmd loop runs this command only after the preceding
        # command handler has returned, so this marker is a completion ack.
        print("RUNNER_ACK=" + token, flush=True)

    def _stop_agents(self, net):
        """Stop only agent subprocesses started and tracked by this CLI instance."""
        errors = []
        remaining = {}
        for host, proc in self.agent_processes.items():
            try:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)
                if proc.poll() is None:
                    remaining[host] = proc
                    errors.append("agent process remains alive on %s" % host)
            except Exception as exc:
                remaining[host] = proc
                errors.append("could not stop agent on %s: %s" % (host, exc))
        self.agent_processes = remaining
        return errors

    def _verified_agent_record(self, host, proc):
        """Wait briefly for this run/host's exact host-visible agent PID."""
        pid = getattr(proc, "pid", None)
        host_pid = self.runtime_state.get("host_pids", {}).get(host)
        if not host_pid and self.mn and host in self.mn:
            host_pid = getattr(self.mn[host], "pid", None)
        run_id = self.runtime_state.get("run_token")
        if not isinstance(pid, int) or pid <= 0 or not host_pid or not run_id:
            return None
        deadline = time.monotonic() + 1.0
        while True:
            details = _read_process_identity(pid)
            if _agent_identity_matches(details, host, run_id, AGENT):
                return {"pid": pid, "host": host, "run_id": run_id,
                        "host_pid": host_pid, "pid_scope": "host",
                        "command": os.path.realpath(AGENT),
                        "start_ticks": details["start_ticks"]}
            if proc.poll() is not None or time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

    @staticmethod
    def _stop_tracked_processes(processes):
        """Stop and reap tracked auxiliary processes; retain any that survive."""
        errors = []
        remaining = []
        for proc in processes:
            try:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)
                if proc.poll() is None:
                    remaining.append(proc)
                    errors.append("tracked process remains alive")
            except Exception as exc:
                remaining.append(proc)
                errors.append("could not stop tracked process: %s" % exc)
        processes[:] = remaining
        return errors

    def _clear_run_state(self, net):
        if net is None:
            raise FaultInjectionError("Mininet network is unavailable for cleanup")
        if getattr(self, "_cleanup_event_logged", False):
            return
        failures = []
        removed_qdiscs = []
        runtime_state = getattr(self, "runtime_state", {
            "active_qdiscs": [], "agents": {}, "iperf": [], "downed_links": []})
        active_hosts = {item.get("host") for item in runtime_state.get("active_qdiscs", [])}
        for host_name in PEERS:
            if host_name not in active_hosts:
                continue
            iface = "%s-eth0" % host_name
            try:
                _run_host_argv(net[host_name], ["tc", "qdisc", "del", "dev", iface,
                                                "root"], timeout=2.0, check_return=False)
                observed = _run_host_argv(net[host_name], ["tc", "qdisc", "show",
                                                           "dev", iface], timeout=2.0).lower()
                if "netem" in observed:
                    failures.append("netem remains on %s" % iface)
                else:
                    # Capture immediately after the verification read.
                    removed_qdiscs.append((host_name, iface, time.time()))
            except Exception as exc:
                failures.append("qdisc cleanup failed on %s: %s" % (iface, exc))
        failures.extend(self._stop_agents(net))
        for sw1, sw2 in list(self.downed):
            try:
                net.configLinkStatus(sw1, sw2, "up")
                links = net.linksBetween(net[sw1], net[sw2])
                if not links or any(not link.intf1.isUp() or not link.intf2.isUp()
                                    for link in links):
                    failures.append("link %s-%s did not return up" % (sw1, sw2))
            except Exception as exc:
                failures.append("link %s-%s cleanup failed: %s" % (sw1, sw2, exc))
        failures.extend(self._stop_tracked_processes(self.active_processes))
        if failures:
            raise FaultInjectionError("cleanup verification failed: " + "; ".join(failures))
        active = runtime_state.get("active_qdiscs", [])
        for host_name, iface, stamp in removed_qdiscs:
            log_cleanup_event("QDISC_REMOVED host=%s interface=%s" % (host_name, iface),
                              timestamp=stamp)
            active[:] = [item for item in active if not (
                item.get("host") == host_name and item.get("interface") == iface)]
        self.downed = []
        runtime_state["agents"] = {}
        runtime_state["iperf"] = []
        runtime_state["downed_links"] = []
        _write_runtime_state(runtime_state)
        log_cleanup_event("FAULT_CLEARED")
        self._cleanup_event_logged = True

    def do_agents(self, line):
        """agents start|stop"""
        net = self.mn
        if line.strip() == "start":
            # Make start idempotent so repeated demos do not create duplicate agents.
            stop_errors = self._stop_agents(net)
            if stop_errors:
                raise RuntimeError("could not stop existing agents: " + "; ".join(stop_errors))
            self._cleanup_event_logged = False
            self.runtime_state["agents"] = {}
            try:
                for h, peer in PEERS.items():
                    proc = net[h].popen(
                        [sys.executable, AGENT, "--id=%s" % h,
                         "--run-id=%s" % self.runtime_state["run_token"],
                         "--peer", net[peer].IP(), "--timeout", "0.6"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.agent_processes[h] = proc
                    record = self._verified_agent_record(h, proc)
                    if record is None:
                        raise RuntimeError("could not verify run-owned telemetry agent on %s" % h)
                    self.runtime_state.setdefault("host_pids", {})[h] = record["host_pid"]
                    self.runtime_state["agents"][h] = record
                    _write_runtime_state(self.runtime_state)
            except Exception as startup_error:
                stop_errors = self._stop_agents(net)
                self.runtime_state["agents"] = {
                    host: record for host, record in self.runtime_state["agents"].items()
                    if host in self.agent_processes
                }
                _write_runtime_state(self.runtime_state)
                if stop_errors:
                    raise RuntimeError("agent startup failed and cleanup was incomplete: "
                                       + "; ".join(stop_errors)) from startup_error
                raise
            log_event("agents started on all six hosts")
        elif line.strip() == "stop":
            stop_errors = self._stop_agents(net)
            if stop_errors:
                raise RuntimeError("could not stop agents: " + "; ".join(stop_errors))
            self.runtime_state["agents"] = {}
            _write_runtime_state(self.runtime_state)
            log_cleanup_event("AGENTS_STOPPED count=%d" % len(PEERS))
        else:
            print("usage: agents start|stop")

    def do_fault(self, line):
        """fault flood|delay|loss|killagent|linkdown|clear ..."""
        a = line.split()
        net = self.mn
        try:
            kind = a[0]
            if kind == "clear":
                self._clear_run_state(net)
                self._last_cleanup_success = True
            else:
                self._cleanup_event_logged = False
                event, activated_at = inject_fault(
                    net, kind, a[1:], {"downed": self.downed,
                                       "iperf": self.active_processes,
                                       "agents": self.agent_processes,
                                       "runtime_state": getattr(self, "runtime_state", {})})
                # Append a success record only after verification, retaining the
                # timestamp captured when the fault actually took effect.
                log_event("FAULT_INJECTED " + event, timestamp=activated_at)
                return
        except Exception as exc:
            if a and a[0] == "clear":
                self._last_cleanup_success = False
                log_cleanup_event("CLEANUP_FAILED: %s" % exc)
                print("[cleanup] FAILED: " + str(exc))
            else:
                message = "FAULT_FAILED %s: %s" % (a[0] if a else "<empty>", exc)
                log_event(message)
                print("[fault] FAILED: " + str(exc))

    def do_exit(self, line):
        """Clean run-owned state before allowing the Mininet CLI to exit."""
        self.do_fault("clear")
        return self._last_cleanup_success

    def do_EOF(self, line):
        return self.do_exit(line)
