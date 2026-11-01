# Person 2 — Ryu monitoring controller

This update is limited to Person 2's controller integration. Existing topology, agent,
fault-injection files, project plan, and teammate files are retained.

## Controller requirements implemented

- OpenFlow 1.3 Ryu application.
- Ryu listens for switches at `127.0.0.1:6653` (the topology's configured remote controller).
- UDP telemetry collector binds to `0.0.0.0:9999`.
- Keeps the existing telemetry JSON interface: `host_id`, `seq`, `timestamp`, `metrics`.
- Validates JSON fields and types, logs malformed messages, detects sequence gaps and
  duplicate/out-of-order sequence numbers.
- Polls OpenFlow flow and port statistics every 10 seconds.
- Learns host MAC locations and uses BFS over discovered switch links for unicast paths.
- Uses a per-packet BFS spanning tree for broadcast/unknown-destination flooding, avoiding
  a broadcast loop in the triangle topology.
- Recomputes paths after discovered link additions/deletions and removes this app's old
  forwarding flows so traffic can use the remaining path.
- Supports `MONITOR_MODE=baseline` and `MONITOR_MODE=dynamic`. Existing threshold-based
  anomaly detection is retained; baseline logs only and dynamic mode applies a temporary
  host-source drop rule.

## Run order

Open terminal 1 (inside the Python 3.9 environment where Ryu is installed):

```bash
cd /path/to/CN-Poject
source ~/miniforge3/bin/activate
conda activate ryu-env
MONITOR_MODE=dynamic ryu-manager --observe-links monitor_controller.py
```

`--observe-links` is important: it enables Ryu's topology link discovery, which this
controller uses to build the triangle graph and calculate BFS routes.

Open terminal 2:

```bash
cd /path/to/CN-Poject
sudo python3 topo.py
```

Wait for the switches to connect and the controller to discover the three inter-switch
links. At the Mininet prompt, run:

```text
mininet> pingall
```

Start telemetry agents at the Mininet prompt:

```text
mininet> agents start
```

To run detection-only baseline mode, stop the controller with Ctrl+C and restart it in
terminal 1 using:

```bash
MONITOR_MODE=baseline ryu-manager --observe-links monitor_controller.py
```

Then restart the topology in terminal 2.

## Ryu environment (Python 3.9)

Use the installation sequence supplied by the teammate:

```bash
python -m venv ~/ryu-venv
source ~/ryu-venv/bin/activate
python -m pip install --upgrade pip
pip install "setuptools<66" wheel pbr
pip install --no-build-isolation ryu eventlet==0.30.2
```

If you already have the working `ryu-env` conda environment, keep using it instead of
creating another environment.

## Notes

- Do not change the agent's telemetry message keys or destination. Agents send to
  `10.0.0.254:9999`; the collector binds to `0.0.0.0:9999`.
- `--observe-links` is required for topology discovery.
- Run the controller before starting Mininet.
- The controller file was syntax-compiled with Python in the packaging environment.
  Live Ryu/Mininet integration and `pingall` must still be verified on the team's
  Ubuntu/Mininet setup; this package does not claim those runtime tests passed.
