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
        super().__init__(*args, **kwargs)
        self.downed = []

    @staticmethod
    def _stop_agents(net):
        """Stop telemetry agents in every host namespace."""
        for host in PEERS:
            # The bracket expression prevents pkill from matching its own shell command.
            net[host].cmd("pkill -f '[a]gent.py' 2>/dev/null || true")

    def do_agents(self, line):
        """agents start|stop"""
        net = self.mn
        if line.strip() == "start":
            # Make start idempotent so repeated demos do not create duplicate agents.
            self._stop_agents(net)
            for h, peer in PEERS.items():
                net[h].cmd("python3 %s --id=%s --peer %s > /dev/null 2>&1 &"
                           % (AGENT, h, net[peer].IP()))
            log_event("agents started on all six hosts")
        elif line.strip() == "stop":
            self._stop_agents(net)
            log_event("agents stopped on all six hosts")
        else:
            print("usage: agents start|stop")

    def do_fault(self, line):
        """fault flood|delay|loss|killagent|linkdown|clear ..."""
        a = line.split()
        net = self.mn
        try:
            kind = a[0]
            if kind == "flood":
                src, dst = net[a[1]], net[a[2]]
                secs = int(a[3]) if len(a) > 3 else 20
                dst.cmd("iperf -s -u > /dev/null 2>&1 &")
                src.cmd("iperf -c %s -u -b 50M -t %d > /dev/null 2>&1 &" % (dst.IP(), secs))
                log_event("flood %s->%s %ds" % (a[1], a[2], secs))
            elif kind == "delay":
                net[a[1]].cmd("tc qdisc replace dev %s-eth0 root netem delay %sms" % (a[1], a[2]))
                log_event("delay %s %sms" % (a[1], a[2]))
            elif kind == "loss":
                net[a[1]].cmd("tc qdisc replace dev %s-eth0 root netem loss %s%%" % (a[1], a[2]))
                log_event("loss %s %s%%" % (a[1], a[2]))
            elif kind == "killagent":
                host = a[1]
                if host not in PEERS:
                    raise KeyError(host)
                # Match the agent command line without matching this pkill shell.
                net[host].cmd("pkill -f '[a]gent.py.*--id=%s' 2>/dev/null || true" % host)
                log_event("killagent %s" % host)
            elif kind == "linkdown":
                net.configLinkStatus(a[1], a[2], "down")
                self.downed.append((a[1], a[2]))
                log_event("linkdown %s-%s" % (a[1], a[2]))
            elif kind == "clear":
                for h in PEERS:
                    net[h].cmd("tc qdisc del dev %s-eth0 root 2>/dev/null" % h)
                    net[h].cmd("pkill iperf 2>/dev/null || true")
                self._stop_agents(net)
                for s1, s2 in self.downed:
                    net.configLinkStatus(s1, s2, "up")
                self.downed = []
                log_event("clear")
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
