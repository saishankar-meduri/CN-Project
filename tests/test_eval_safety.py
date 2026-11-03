import os
import json
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from faults import test_runner as runner


class EvaluationSafetyTests(unittest.TestCase):
    def test_fault_timestamp_requires_one_exact_success_event(self):
        line = "1700000000.123 FAULT_INJECTED delay h2 200"
        self.assertEqual(runner.extract_fault_timestamp([line], "fault delay h2 200")[0],
                         1700000000.123)

    def test_fault_timestamp_rejects_missing_event_and_cleanup_fallback(self):
        with self.assertRaises(ValueError):
            runner.extract_fault_timestamp(["1700000001.0 clear"], "fault delay h2 200")

    def test_fault_timestamp_rejects_duplicate_events(self):
        lines = ["1700000000.1 FAULT_INJECTED loss h3 30",
                 "1700000001.1 FAULT_INJECTED loss h3 30"]
        with self.assertRaises(ValueError):
            runner.extract_fault_timestamp(lines, "fault loss h3 30")

    def test_cleanup_event_is_separate_from_injection_timestamp(self):
        self.assertTrue(runner.verified_clear_event(["1700000010.0 FAULT_CLEARED"]))
        self.assertFalse(runner.verified_clear_event(["1700000010.0 clear"]))
        with self.assertRaises(ValueError):
            runner.extract_fault_timestamp(["1700000010.0 FAULT_CLEARED"],
                                           "fault delay h2 200")

    def test_wrong_host_action_is_not_attributed(self):
        scenario = {"id": "killagent"}
        lines = [
            "[1700000000.000] [WARNING] ANOMALY DETECTED [AGENT CRASH] on h5: telemetry missing",
            "[1700000002.000] [ACTION] DYNAMIC ACTION [AGENT CRASH]: alert for host 'h4'",
        ]
        _, _, action_ts, action = runner.parse_controller_anomaly(lines, 1699999999.0,
                                                                   scenario)
        self.assertIsNone(action_ts)
        self.assertIsNone(action)

    def test_pre_injection_anomaly_is_not_counted(self):
        scenario = {"id": "killagent"}
        lines = ["[1700000000.000] [WARNING] ANOMALY DETECTED [AGENT CRASH] on h5: telemetry missing"]
        detection, latency, action_ts, _ = runner.parse_controller_anomaly(
            lines, 1700000000.1, scenario)
        self.assertIsNone(detection)
        self.assertIsNone(latency)
        self.assertIsNone(action_ts)

    def test_multi_host_action_is_ambiguous(self):
        scenario = {"id": "killagent"}
        lines = [
            "[1700000000.000] [WARNING] ANOMALY DETECTED [AGENT CRASH] on h5: telemetry missing",
            "[1700000001.000] [ACTION] DYNAMIC ACTION [AGENT CRASH]: alert for hosts h5 and h4",
        ]
        _, _, action_ts, _ = runner.parse_controller_anomaly(lines, 1699999999.0,
                                                              scenario)
        self.assertIsNone(action_ts)

    def test_matching_first_action_is_attributed(self):
        scenario = {"id": "killagent"}
        lines = [
            "[1700000000.000] [WARNING] ANOMALY DETECTED [AGENT CRASH] on h5: telemetry missing",
            "[1700000001.000] [ACTION] DYNAMIC ACTION [AGENT CRASH]: alert for host 'h5'",
            "[1700000002.000] [ACTION] DYNAMIC ACTION [AGENT CRASH]: alert for host 'h5'",
        ]
        _, _, action_ts, _ = runner.parse_controller_anomaly(lines, 1699999999.0,
                                                              scenario)
        self.assertEqual(action_ts, 1700000001.0)

    def test_stale_startup_marker_is_rejected(self):
        with self.assertRaises(RuntimeError):
            runner.validate_startup_marker(
                [(100.0, "SDN Controller initialized. Mode: [BASELINE]")],
                "baseline", 200.0)

    def test_wrong_mode_startup_marker_is_rejected(self):
        with self.assertRaises(RuntimeError):
            runner.validate_startup_marker(
                [(201.0, "SDN Controller initialized. Mode: [DYNAMIC]")],
                "baseline", 200.0)

    def test_baseline_and_dynamic_directories_do_not_collide(self):
        with tempfile.TemporaryDirectory() as root:
            baseline = runner.mode_run_directory(root, "baseline", "same-run")
            dynamic = runner.mode_run_directory(root, "dynamic", "same-run")
            self.assertNotEqual(baseline, dynamic)
            with self.assertRaises(FileExistsError):
                runner.mode_run_directory(root, "baseline", "same-run")

    def test_timeout_cleanup_signals_only_the_owned_group(self):
        proc = Mock()
        proc.wait.side_effect = [subprocess.TimeoutExpired("topo", 5),
                                 subprocess.TimeoutExpired("topo", 3),
                                 None]
        proc.poll.return_value = 0
        with patch.object(runner.os, "killpg") as killpg:
            self.assertTrue(runner.terminate_owned_process_group(proc, 424242))
        self.assertEqual([call.args for call in killpg.call_args_list], [
            (424242, runner.signal.SIGINT), (424242, runner.signal.SIGTERM),
            (424242, runner.signal.SIGKILL)])

    def test_missing_measurements_remain_null(self):
        summary = runner.summarize_measurements([])
        for phase in summary.values():
            self.assertIsNone(phase["rtt_ms"])
            self.assertIsNone(phase["packet_loss_pct"])
            self.assertIsNone(phase["host_interface_aggregate_mbps"])

    def test_recovery_requires_independent_healthy_samples_and_baseline(self):
        self.assertIsNone(runner.find_service_recovery([], 100.0, []))
        baseline = [{"rtt_ms": 10.0, "packet_loss_pct": 0}]
        one_good = [{"phase": "post_clear", "timestamp": 120.0,
                     "rtt_ms": 10.0, "packet_loss_pct": 0}]
        self.assertIsNone(runner.find_service_recovery(one_good, 100.0, baseline))
        two_good = one_good + [{"phase": "post_clear", "timestamp": 121.0,
                                "rtt_ms": 12.0, "packet_loss_pct": 0}]
        self.assertIsNone(runner.find_service_recovery(two_good, 100.0, baseline))
        self.assertEqual(runner.find_service_recovery(two_good, 100.0, baseline,
                                                      cleanup_ts=110.0), 21.0)
        in_fault = [{"phase": "fault", "timestamp": 111.0,
                     "rtt_ms": 11.0, "packet_loss_pct": 0},
                    {"phase": "fault", "timestamp": 112.0,
                     "rtt_ms": 12.0, "packet_loss_pct": 0}]
        self.assertIsNone(runner.find_service_recovery(in_fault, 100.0, baseline,
                                                       cleanup_ts=105.0))


class FaultInjectionVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # faults.py imports Mininet CLI only to define its extension class.
        mininet = types.ModuleType("mininet")
        cli = types.ModuleType("mininet.cli")
        cli.CLI = type("CLI", (), {})
        sys.modules.setdefault("mininet", mininet)
        sys.modules.setdefault("mininet.cli", cli)
        from faults import faults
        cls.faults = faults

    class AgentProcess:
        _next_pid = 1000

        def __init__(self):
            self.alive = True
            self.pid = FaultInjectionVerificationTests.AgentProcess._next_pid
            FaultInjectionVerificationTests.AgentProcess._next_pid += 1
            self.argv = []

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.alive = False

        def kill(self):
            self.alive = False

        def wait(self, timeout=None):
            if self.alive:
                raise subprocess.TimeoutExpired("agent", timeout)
            return 0

    class AgentHost:
        def __init__(self, host_id):
            self.host_id = host_id
            self.processes = []
            self.commands = []
            self.pid = 5000 + int(host_id[1:])

        def popen(self, *args, **kwargs):
            process = FaultInjectionVerificationTests.AgentProcess()
            process.argv = args[0]
            self.processes.append(process)
            self.commands.append(args[0])
            return process

        def IP(self):
            return "10.0.0.%d" % (int(self.host_id[1:]) + 1)

    @classmethod
    def _agent_net(cls):
        return {"h%d" % number: cls.AgentHost("h%d" % number)
                for number in range(1, 7)}

    def _fake_agent_process_details(self, net, pid):
        for host in net.values():
            for proc in host.processes:
                if proc.pid == pid:
                    return {"argv": [sys.executable] + proc.argv,
                            "state": "S", "start_ticks": proc.pid * 10}
        return None

    def test_state_exists_before_cli_loop_and_agents_are_tracked(self):
        net = self._agent_net()

        def enter_cli(instance, mininet):
            for attr in ("downed", "active_processes", "agent_processes", "measurement_counters"):
                self.assertTrue(hasattr(instance, attr), "missing state before cmdloop: " + attr)
            instance.mn = mininet
            instance.do_agents("start")

        with tempfile.TemporaryDirectory() as temp:
            manifest = os.path.join(temp, "runtime.json")
            with patch.object(self.faults, "RUNTIME_STATE_FILE", manifest), \
                    patch.object(self.faults.CLI, "__init__", enter_cli), \
                    patch.object(self.faults, "_read_process_identity",
                                 side_effect=lambda pid: self._fake_agent_process_details(net, pid)):
                with patch.object(self.faults, "log_event"), \
                        patch.object(self.faults, "log_cleanup_event"):
                    cli = self.faults.FaultCLI(net)
            with open(manifest, encoding="utf-8") as f:
                saved_state = json.load(f)

        self.assertEqual(set(cli.agent_processes), set(self.faults.PEERS))
        self.assertEqual(sum(len(host.processes) for host in net.values()), 6)
        self.assertTrue(all(proc.alive for proc in cli.agent_processes.values()))
        self.assertEqual(set(cli.runtime_state["agents"]), set(self.faults.PEERS))
        for host, record in cli.runtime_state["agents"].items():
            self.assertEqual(record["host"], host)
            self.assertEqual(record["run_id"], cli.runtime_state["run_token"])
            self.assertEqual(record["pid"], cli.agent_processes[host].pid)
            self.assertEqual(record["pid_scope"], "host")
            self.assertIn("--run-id=" + record["run_id"], net[host].commands[0])
        self.assertEqual(set(saved_state["agents"]), set(self.faults.PEERS))
        self.assertTrue(all(saved_state["agents"][host]["pid"] ==
                            cli.agent_processes[host].pid for host in self.faults.PEERS))
        self.assertEqual(set(saved_state["host_pids"]), set(self.faults.PEERS))

    def test_agents_stop_terminates_every_tracked_agent(self):
        net = self._agent_net()

        def enter_cli(instance, mininet):
            instance.mn = mininet
            instance.do_agents("start")

        with patch.object(self.faults.CLI, "__init__", enter_cli), \
                patch.object(self.faults, "_read_process_identity",
                             side_effect=lambda pid: self._fake_agent_process_details(net, pid)):
            with patch.object(self.faults, "log_event"), \
                    patch.object(self.faults, "log_cleanup_event"):
                cli = self.faults.FaultCLI(net)
                tracked = list(cli.agent_processes.values())
                cli.do_agents("stop")

        self.assertEqual(cli.agent_processes, {})
        self.assertTrue(all(proc.poll() is not None for proc in tracked))
        self.assertEqual(cli.runtime_state["agents"], {})

    def test_unverified_agent_start_rolls_back_all_started_agents(self):
        net = self._agent_net()

        def enter_cli(instance, mininet):
            instance.mn = mininet
            instance.do_agents("start")

        def fail_h3(pid):
            for host, node in net.items():
                for proc in node.processes:
                    if proc.pid == pid:
                        if host == "h3":
                            return None
                        return {"argv": [sys.executable] + proc.argv,
                                "state": "S", "start_ticks": proc.pid * 10}
            return None

        with patch.object(self.faults.CLI, "__init__", enter_cli), \
                patch.object(self.faults, "_read_process_identity", side_effect=fail_h3), \
                patch.object(self.faults, "log_event"), \
                patch.object(self.faults, "log_cleanup_event"):
            with self.assertRaisesRegex(RuntimeError, "could not verify run-owned"):
                self.faults.FaultCLI(net)

        processes = [proc for host in net.values() for proc in host.processes]
        self.assertEqual(len(processes), 3)
        self.assertTrue(all(proc.poll() is not None for proc in processes))

    def test_cli_initialization_failure_reaps_agents_started_in_loop(self):
        net = self._agent_net()
        auxiliary_processes = []

        def fail_after_command_loop_begins(instance, mininet):
            instance.mn = mininet
            instance.do_agents("start")
            process = self.AgentProcess()
            instance.active_processes.append(process)
            auxiliary_processes.append(process)
            raise RuntimeError("simulated CLI initialization failure")

        with patch.object(self.faults.CLI, "__init__", fail_after_command_loop_begins), \
                patch.object(self.faults, "_read_process_identity",
                             side_effect=lambda pid: self._fake_agent_process_details(net, pid)):
            with patch.object(self.faults, "log_event"), \
                    patch.object(self.faults, "log_cleanup_event"):
                with self.assertRaisesRegex(RuntimeError, "simulated CLI initialization failure"):
                    self.faults.FaultCLI(net)

        processes = [proc for host in net.values() for proc in host.processes]
        self.assertEqual(len(processes), 6)
        self.assertTrue(all(proc.poll() is not None for proc in processes))
        self.assertEqual(len(auxiliary_processes), 1)
        self.assertIsNotNone(auxiliary_processes[0].poll())

    def test_failed_tc_verification_raises_without_success_event(self):
        node = Mock()
        node.cmd.side_effect = ["", "priomap 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15"]
        with self.assertRaises(self.faults.FaultInjectionError):
            self.faults.inject_fault({"h2": node}, "delay", ["h2", "200"], {})

    def test_failed_cli_injection_records_failure_only(self):
        node = Mock()
        node.cmd.side_effect = ["", "no netem qdisc"]
        cli = self.faults.FaultCLI.__new__(self.faults.FaultCLI)
        cli.mn = {"h2": node}
        cli.downed = []
        cli.active_processes = []
        cli.agent_processes = {}
        with patch.object(self.faults, "log_event") as log_event:
            cli.do_fault("delay h2 200")
        payload = log_event.call_args.args[0]
        self.assertTrue(payload.startswith("FAULT_FAILED"))
        self.assertNotIn("FAULT_INJECTED", payload)


if __name__ == "__main__":
    unittest.main()
