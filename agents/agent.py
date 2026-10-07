#!/usr/bin/env python3
"""Host telemetry agent.

Each host runs one agent. It:
  - runs a UDP echo server (port 9998) so peers can probe it
  - probes its peer every interval, measuring real RTT and timeouts,
    so tc netem delay/loss and floods show up in the metrics
  - sends one JSON datagram per interval to the controller collector

Usage (inside a Mininet host):
    python3 agent.py h1 --peer 10.0.0.4
"""
import argparse
import json
import socket
import threading
import time

try:
    import psutil
except ImportError:  # CPU/mem fall back to 0.0
    psutil = None

ECHO_PORT = 9998


def echo_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", ECHO_PORT))
    while True:
        data, addr = s.recvfrom(1024)
        s.sendto(data, addr)


def probe(peer, n, timeout):
    """Send n UDP probes to the peer's echo server.
    Returns (avg_rtt_ms, ok_count, error_count)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtts, errors = [], 0
    for i in range(n):
        payload = b"p%d" % i
        t0 = time.time()
        deadline = t0 + timeout
        got = False
        try:
            s.sendto(payload, (peer, ECHO_PORT))
            while not got:
                s.settimeout(max(0.001, deadline - time.time()))
                data, _ = s.recvfrom(1024)
                got = data == payload  # ignore late replies to old probes
        except (socket.timeout, OSError):
            pass
        if got:
            rtts.append((time.time() - t0) * 1000)
        else:
            errors += 1
    s.close()
    avg = sum(rtts) / len(rtts) if rtts else timeout * 1000
    return avg, len(rtts), errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", dest="host_id", required=True)
    ap.add_argument("--peer", help="IP of the host to probe")
    ap.add_argument("--collector", default="10.0.0.254")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--probes", type=int, default=5)
    ap.add_argument("--timeout", type=float, default=0.2)
    a = ap.parse_args()

    threading.Thread(target=echo_server, daemon=True).start()
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if psutil:
        psutil.cpu_percent(interval=None)  # prime the counter

    seq = 0
    while True:
        start = time.time()
        if a.peer:
            lat, ok, err = probe(a.peer, a.probes, a.timeout)
            rps = ok / max(time.time() - start, 0.001)
        else:
            lat, rps, err = 0.0, 0.0, 0

        msg = {
            "host_id": a.host_id,
            "seq": seq,
            "timestamp": time.time(),
            "metrics": {
                "app_latency_ms": round(lat, 2),
                "req_per_sec": round(rps, 1),
                "error_count": err,  # probe timeouts this interval
                "cpu_percent": psutil.cpu_percent(interval=None) if psutil else 0.0,
                "mem_percent": psutil.virtual_memory().percent if psutil else 0.0,
            },
        }
        try:
            out.sendto(json.dumps(msg).encode(), (a.collector, a.port))
        except OSError:
            pass  # network down: keep running, controller sees missing telemetry
        seq += 1
        time.sleep(max(0.0, a.interval - (time.time() - start)))


if __name__ == "__main__":
    main()
