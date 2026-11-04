# Arch Linux Evaluation Setup

## Environment audit

Audited on 2026-11-03 (Asia/Kolkata):

| Component | Observed |
|---|---|
| OS / kernel | Arch Linux, x86_64; verified live kernel 7.2.14-arch1-1 |
| System Python | 3.14.9 |
| Mininet | 2.3.1b4; imports under system Python 3.14 |
| Open vSwitch | 4.0.0; `ovsdb-server` and `ovs-vswitchd` confirmed active |
| `iperf` / `iproute2` | 2.2.1 / 7.3.0 |
| `psutil` | 7.2.2 installed for system Python |
| Ryu | 4.34 and dependencies were available in the local uv cache; no Arch pacman Ryu package was found |
| Compatible Python | Python 3.10.21 was already cached under uv; Python 3.14 remains the system interpreter |
| Kernel module state | `veth` loaded successfully on the verified kernel |
| Mininet connectivity | Basic `pingall` passed; project topology later passed all-host and NAT connectivity checks |

Mininet, its Python modules, `psutil`, `iperf`, `tc`, and OVS command line utilities were found. Imports for Mininet, the topology dependencies, the agent, and fault CLI succeeded. Python syntax compilation succeeded. Ryu is not compatible with this host's system Python 3.14. In the Python 3.10 virtual environment, Ryu 4.34 needs `EVENTLET_NO_GREENDNS=yes` and an Eventlet `ALREADY_HANDLED` compatibility sentinel; `tools/ryu_manager.py` applies these without changing controller detection or response logic.

An earlier attempt on kernel `7.2.9-arch1-1` failed to create veth devices. After booting kernel `7.2.14-arch1-1`, `veth` loaded successfully. A basic Mininet `pingall` passed, and the repaired project topology was subsequently verified with all six hosts, NAT-to-host connectivity, and 0% loss in `pingall`. These are connectivity checks, not fault-evaluation measurements. No real fault-evaluation run is authorized or reported by this document update. Controller startup alarms emitted before agent telemetry is established must be excluded from fault timing.

## Set up on Arch

Keep system Python unchanged. The audited host already has Python 3.10.21 via uv. On another Arch host, install uv using the user's preferred method, then let uv obtain a Python 3.10 interpreter. This does not replace system Python:

```bash
uv python install 3.10
uv venv --python 3.10 .venv
uv pip install --python .venv/bin/python -r requirements-ryu.txt
```

If Mininet/OVS tools are absent, first review the package transaction and then install the Arch packages:

```bash
sudo pacman -S mininet openvswitch iperf iproute2 python-psutil
```

Inspect the installed Open vSwitch unit names and state before starting services. For example:

```bash
pacman -Ql openvswitch | rg 'systemd/system/.*(ovs|openvswitch)'
systemctl list-unit-files '*ovs*' '*openvswitch*'
systemctl status ovsdb-server.service ovs-vswitchd.service --no-pager
```

If the installed unit names match those above and are inactive, enable/start the units according to the package's service dependencies. Do not assume service names from another Arch package release.

If Mininet reports an unknown veth device, check that the running kernel and installed module tree match, then load the matching module if needed:

```bash
uname -r
sudo modprobe veth
lsmod | rg '^veth\b'
```

Expected kernel version after reboot is `7.2.14-arch1-1`. If the machine must remain on the older running kernel, install the matching Arch kernel/modules package for that kernel version instead of trying to load a module built for a different kernel. No kernel package was installed or changed during this evaluation.

Start Ryu from the project virtual environment:

```bash
.venv/bin/python tools/ryu_manager.py controller/sdn_controller.py --mode=baseline
# Stop Ryu, then start it in a separate baseline/dynamic batch as needed.
.venv/bin/python tools/ryu_manager.py controller/sdn_controller.py --mode=dynamic
```

The topology uses system `python3` because Arch's Mininet modules are installed there. The live runner requires root to create namespaces, links, traffic-control rules, and OVS switches; run it only after approval. The runner records verified injection events, independent one-echo RTT/loss samples, and aggregate host-interface byte-counter throughput. Recovery is recorded only after two consecutive post-injection probes meet the run-relative health threshold, during the fault or after clear. These measurements are limited: one-echo loss is coarse, and interface aggregate throughput is not flow-specific goodput. See `README.md` and `docs/report.md` for the precise method.

## Current evaluation status

Live baseline and dynamic evaluations were completed on 2026-11-04 (one run per scenario and mode, 5/5 faults detected in each mode); raw logs and summaries are under `logs/evaluation_prepared_20261104_v3/`. Pre-existing dry-run and synthetic files in `logs/` are legacy artifacts and are not measurements.
