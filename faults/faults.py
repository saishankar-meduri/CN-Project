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
    downed = []

    def do_agents(self, line):
        """agents start|stop"""
        net = self.mn
        if line.strip() == "start":
            for h, peer in PEERS.items():
                net[h].cmd("python3 %s --id=%s --peer %s > /dev/null 2>&1 &"
                           % (AGENT, h, net[peer].IP()))
            log_event("agents started")
        elif line.strip() == "stop":
            net["h1"].cmd("pkill -f agent.py")
            log_event("agents stopped")
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
                net[a[1]].cmd("pkill -f -- '--id=%s'" % a[1])
                log_event("killagent %s" % a[1])
            elif kind == "linkdown":
                net.configLinkStatus(a[1], a[2], "down")
                self.downed.append((a[1], a[2]))
                log_event("linkdown %s-%s" % (a[1], a[2]))
            elif kind == "clear":
                for h in ("h1", "h2", "h3", "h4", "h5", "h6"):
                    net[h].cmd("tc qdisc del dev %s-eth0 root 2>/dev/null" % h)
                net["h1"].cmd("pkill iperf")
                for s1, s2 in self.downed:
                    net.configLinkStatus(s1, s2, "up")
                self.downed = []
                log_event("clear")
            else:
                print(self.do_fault.__doc__)
        except (IndexError, KeyError, ValueError):
            print("bad arguments. see: help fault")
