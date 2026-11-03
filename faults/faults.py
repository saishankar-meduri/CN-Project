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
import time
from mininet.cli import CLI

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.join(HERE, "..", "agents", "agent.py")
LOGFILE = os.path.join(HERE, "..", "logs", "fault_events.log")
# each host probes a host on a different switch
PEERS = {"h1": "h4", "h2": "h5", "h3": "h6", "h4": "h1", "h5": "h2", "h6": "h3"}


def log_event(text):
    os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
    line = "%.3f %s" % (time.time(), text)
    with open(LOGFILE, "a") as f:
        f.write(line + "\n")
    print("[fault] " + line)


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
                print(self.do_fault.__doc__)
        except (IndexError, KeyError, ValueError):
            print("bad arguments. see: help fault")
