import contextlib
import cmd
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

mininet_module = types.ModuleType("mininet")
cli_module = types.ModuleType("mininet.cli")
cli_module.CLI = type("CLI", (), {})
sys.modules.setdefault("mininet", mininet_module)
sys.modules.setdefault("mininet.cli", cli_module)

from faults import faults
from faults import test_runner


class FakeProcess:
    def __init__(self, output="", timeout_once=False, pid=8123):
        self.output = output
        self.timeout_once = timeout_once
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.killed = False

    def communicate(self, timeout=None):
        if self.timeout_once and not self.terminated:
            raise subprocess.TimeoutExpired("fake", timeout)
        self.returncode = 0
        return self.output, ""

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.terminated = True


class FakeNode:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.commands = []

    def popen(self, argv, **kwargs):
        self.commands.append(argv)
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        return output

    def IP(self):
        return "10.0.0.5"


class FakeStdin:
    def __init__(self, runner, respond=True):
        self.runner = runner
        self.respond = respond
        self.buffer = ""
        self.writes = []

    def write(self, value):
        self.buffer += value
        self.writes.append(value)

    def flush(self):
        if not self.respond:
            return
        command, ack_command = self.buffer.splitlines()[:2]
        if not ack_command.startswith("runner_ack "):
            raise AssertionError("runner must use Mininet's underscore command name")
        token = ack_command.split(None, 1)[1]
        self.runner.completed_commands.append(command)
        if "measure " in self.buffer:
            self.runner.stdout_lines.append(
                'MEASURE_JSON={"rtt_ms": 1.0, "packet_loss_pct": 0.0}')
        self.runner.stdout_lines.append("RUNNER_ACK=" + token)
        self.runner.ack_emitted_after_command = bool(self.runner.completed_commands)
        with self.runner._condition:
            self.runner._condition.notify_all()
        self.buffer = ""


class FakeRunnerProcess:
    def __init__(self, runner, respond=True):
        self.stdin = FakeStdin(runner, respond)
        self.returncode = None
        runner.completed_commands = []
        runner.ack_emitted_after_command = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake-mininet", timeout)
        return self.returncode


class CliMeasurementTests(unittest.TestCase):
    def make_cli(self, host):
        cli = faults.FaultCLI.__new__(faults.FaultCLI)
        cli.mn = {"h2": host, "h5": host}
        cli.measurement_counters = {}
        return cli

    def test_measure_command_completes_and_emits_json(self):
        host = FakeNode([FakeProcess("1 received\nrtt min/avg/max/mdev = 1/2/3/4\n"),
                         FakeProcess("100\n200\n")])
        cli = self.make_cli(host)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.do_measure("h2 h5")
        self.assertIn("MEASURE_JSON=", output.getvalue())
        self.assertEqual(len(host.commands), 2)
        self.assertTrue(all(isinstance(argv, list) for argv in host.commands))

    def test_measure_subprocess_timeout_is_bounded_and_reaped(self):
        timed_out = FakeProcess(timeout_once=True)
        host = FakeNode([timed_out])
        cli = self.make_cli(host)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.do_measure("h2 h5")
        self.assertTrue(timed_out.terminated)
        self.assertIn("MEASURE_ERROR=", output.getvalue())

    def test_runner_measurement_waits_for_ack_and_times_out(self):
        runner = test_runner.MininetSubprocessRunner("topo.py", ".", outdir=tempfile.gettempdir())
        runner.proc = FakeRunnerProcess(runner, respond=True)
        self.assertEqual(runner.measure("h2", "h5")["rtt_ms"], 1.0)

        stalled = test_runner.MininetSubprocessRunner("topo.py", ".",
                                                       outdir=tempfile.gettempdir())
        stalled.proc = FakeRunnerProcess(stalled, respond=False)
        stalled.measure_timeout_sec = 0.01
        self.assertIsNone(stalled.measure("h2", "h5"))
        self.assertFalse(stalled.command_channel_healthy)
        writes_before = stalled.proc.stdin.buffer
        self.assertFalse(stalled.send_command("fault clear", wait_sec=0.01))
        self.assertEqual(stalled.proc.stdin.buffer, writes_before)

    def test_runner_ack_command_is_emitted_after_command_and_uses_unique_token(self):
        runner = test_runner.MininetSubprocessRunner("topo.py", ".",
                                                     outdir=tempfile.gettempdir())
        runner.proc = FakeRunnerProcess(runner)
        self.assertTrue(runner.send_command("pingall", wait_sec=1))
        self.assertEqual(runner.completed_commands, ["pingall"])
        self.assertTrue(runner.ack_emitted_after_command)
        ack_command = "".join(runner.proc.stdin.writes).splitlines()[1]
        token = ack_command.split()[-1]
        self.assertEqual(ack_command.split()[0], "runner_ack")
        self.assertRegex(token, r"^[0-9a-f]{32}$")

    def test_ack_parser_rejects_wrong_malformed_and_duplicate_acknowledgments(self):
        token = "a" * 32
        self.assertTrue(test_runner.validate_runner_acknowledgments(
            ["mininet> RUNNER_ACK=" + token], token))
        with self.assertRaisesRegex(ValueError, "mismatched"):
            test_runner.validate_runner_acknowledgments(["RUNNER_ACK=" + "b" * 32], token)
        with self.assertRaisesRegex(ValueError, "malformed"):
            test_runner.validate_runner_acknowledgments(["RUNNER_ACK=bad"], token)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            test_runner.validate_runner_acknowledgments(
                ["RUNNER_ACK=" + token, "RUNNER_ACK=" + token], token)

    def test_ordinary_mininet_output_is_not_an_acknowledgment(self):
        self.assertFalse(test_runner.validate_runner_acknowledgments(
            ["*** Results: 0% dropped (42/42 received)",
             "Unknown command: runner_ack deadbeef"], "a" * 32))

    def test_faultcli_ack_is_registered_under_mininet_command_name(self):
        class ParserCLI(cmd.Cmd, faults.FaultCLI):
            pass

        cli = ParserCLI()
        token = "c" * 32
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.onecmd("runner_ack " + token)
        self.assertEqual(output.getvalue(), "RUNNER_ACK=" + token + "\n")

    def test_faultcli_rejects_malformed_ack_token(self):
        class ParserCLI(cmd.Cmd, faults.FaultCLI):
            pass

        cli = ParserCLI()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.onecmd("runner_ack not-a-token")
        self.assertEqual(output.getvalue(), "RUNNER_ACK_ERROR=malformed-token\n")

    def test_pingall_requires_all_42_replies(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = test_runner.MininetSubprocessRunner("topo.py", temp, outdir=temp)
            runner.stdout_lines = []

            def complete_pingall(*args, **kwargs):
                runner.stdout_lines.append("*** Results: 0% dropped (42/42 received)")
                return True

            with patch.object(runner, "send_command", side_effect=complete_pingall):
                self.assertTrue(runner.pingall())
            runner.stdout_lines = ["*** Results: 2% dropped (41/42 received)"]
            with patch.object(runner, "send_command", return_value=True):
                self.assertFalse(runner.pingall())

    def test_agent_verification_requires_six_owned_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = test_runner.MininetSubprocessRunner("topo.py", temp, outdir=temp)
            agent_script = os.path.realpath(test_runner.DEFAULT_AGENT_SCRIPT)
            host_pids = {"h%d" % number: 7000 + number for number in range(1, 7)}
            details = {}
            agents = {}
            for number in range(1, 7):
                host = "h%d" % number
                pid = 8000 + number
                ticks = 9000 + number
                agents[host] = {"pid": pid, "host": host,
                                "run_id": runner.run_token,
                                "host_pid": host_pids[host],
                                "pid_scope": "host",
                                "command": agent_script, "start_ticks": ticks}
                details[pid] = {"argv": ["/usr/bin/python3.14", agent_script,
                                         "--id=" + host,
                                         "--run-id=" + runner.run_token],
                                "state": "S", "start_ticks": ticks}
            state = {"run_token": runner.run_token, "host_pids": host_pids,
                     "agents": agents}
            with open(runner.runtime_state_path, "w", encoding="utf-8") as f:
                json.dump(state, f)
            with patch.object(test_runner, "read_host_process_identity",
                              side_effect=lambda pid: details.get(pid)):
                self.assertTrue(runner.agents_running())
                state["agents"]["h1"]["run_id"] = "wrong-run"
                with open(runner.runtime_state_path, "w", encoding="utf-8") as f:
                    json.dump(state, f)
                self.assertFalse(runner.agents_running())
                state["agents"]["h1"]["run_id"] = runner.run_token
                details[8006] = None  # exited/missing h6
                with open(runner.runtime_state_path, "w", encoding="utf-8") as f:
                    json.dump(state, f)
                self.assertFalse(runner.agents_running())
                details[8006] = {"argv": [], "state": "Z", "start_ticks": 9006}
                self.assertFalse(runner.agents_running())
            state["agents"].pop("h6")
            with open(runner.runtime_state_path, "w", encoding="utf-8") as f:
                json.dump(state, f)
            self.assertFalse(runner.agents_running())


class InterpreterSelectionTests(unittest.TestCase):
    def test_topology_interpreter_auto_selects_first_mininet_capable_candidate(self):
        outcomes = {"/candidate/one": RuntimeError("missing mininet"),
                    "/candidate/two": "/real/python3.14"}

        def verify(path, modules, role, **kwargs):
            result = outcomes[path]
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(test_runner, "_resolve_executable",
                          side_effect=lambda value: value), \
                patch.object(test_runner.sys, "executable", "/candidate/one"), \
                patch.object(test_runner.shutil, "which",
                             side_effect=lambda name: "/candidate/two" if name == "python3" else None), \
                patch.object(test_runner, "verify_python_imports", side_effect=verify):
            self.assertEqual(test_runner.select_topology_interpreter(), "/real/python3.14")

    def test_configured_topology_import_failure_is_reported(self):
        with patch.object(test_runner, "_resolve_executable", return_value="/custom/python"), \
                patch.object(test_runner, "verify_python_imports",
                             side_effect=RuntimeError("cannot import mininet")):
            with self.assertRaisesRegex(RuntimeError, "cannot import mininet"):
                test_runner.select_topology_interpreter("/custom/python")

    def test_controller_uses_python310_and_checks_ryu_imports(self):
        with patch.object(test_runner, "verify_python_imports", return_value="/venv/bin/python") as verify:
            self.assertEqual(test_runner.validate_controller_interpreter("/venv/bin/python"),
                             "/venv/bin/python")
        args = verify.call_args.args
        self.assertIn("ryu.cmd.manager", args[1])
        self.assertEqual(verify.call_args.kwargs["required_version"], (3, 10))

    def test_sudo_topology_command_preserves_explicit_interpreter_and_run_environment(self):
        env = {"PYTHONUNBUFFERED": "1", "SDN_RUN_TOKEN": "token",
               "SDN_FAULT_LOG": "/tmp/fault", "SDN_CLEANUP_LOG": "/tmp/cleanup",
               "SDN_RUNTIME_STATE_FILE": "/tmp/state"}
        command = test_runner.topology_launch_command(
            "/usr/bin/python3.14", "/repo/topology/topo.py", env, euid=1000)
        self.assertEqual(command[0:3], ["sudo", "-E", "--"])
        self.assertEqual(command[-2:], ["/usr/bin/python3.14", "/repo/topology/topo.py"])
        self.assertIn("SDN_RUN_TOKEN=token", command)

    def test_controller_log_rejects_wrong_listener_interpreter(self):
        with patch.object(test_runner.os.path, "realpath", side_effect=lambda path: path):
            with self.assertRaisesRegex(RuntimeError, "controller listener uses /other/python"):
                test_runner.verify_interpreter_match(
                    "/other/python", "/venv/bin/python", "controller listener")


class CleanupPathTests(unittest.TestCase):
    def test_missing_ack_timeout_invokes_scoped_emergency_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = test_runner.MininetSubprocessRunner("topo.py", temp, outdir=temp)
            runner.proc = FakeRunnerProcess(runner, respond=False)
            runner.process_group_id = 7777
            runner.stdout_lines = ["MININET_NET_STOPPED"]
            runner.emergency_cleanup = Mock(return_value=True)
            runner._finalize_namespace_cleanup = Mock(return_value=True)

            def terminate(proc, pgid):
                proc.returncode = 0
                return True

            with patch.object(test_runner, "terminate_owned_process_group",
                              side_effect=terminate), \
                    patch.object(test_runner.os, "killpg", side_effect=ProcessLookupError):
                self.assertFalse(runner._send_acknowledged("pingall", 0.01))
                self.assertFalse(runner.command_channel_healthy)
                self.assertTrue(runner.stop())
            runner.emergency_cleanup.assert_called_once_with()

    def test_cleanup_events_are_written_separately_from_injections(self):
        with tempfile.TemporaryDirectory() as temp:
            fault_path = os.path.join(temp, "fault_events.log")
            cleanup_path = os.path.join(temp, "cleanup_events.log")
            with patch.object(faults, "LOGFILE", fault_path), \
                    patch.object(faults, "CLEANUP_LOGFILE", cleanup_path):
                faults.log_event("FAULT_INJECTED delay h2 200", timestamp=10.0)
                faults.log_cleanup_event("QDISC_REMOVED host=h2 interface=h2-eth0",
                                         timestamp=11.0)
            with open(fault_path, encoding="utf-8") as f:
                fault_contents = f.read()
            with open(cleanup_path, encoding="utf-8") as f:
                cleanup_contents = f.read()
        self.assertIn("FAULT_INJECTED", fault_contents)
        self.assertNotIn("QDISC_REMOVED", fault_contents)
        self.assertIn("QDISC_REMOVED", cleanup_contents)
        self.assertNotIn("FAULT_INJECTED", cleanup_contents)

    def test_qdisc_removal_timestamp_is_written_after_verified_absence(self):
        host = FakeNode([FakeProcess(""), FakeProcess("qdisc noqueue 0: root\n")])
        cli = faults.FaultCLI.__new__(faults.FaultCLI)
        cli.mn = {"h2": host}
        cli.downed = []
        cli.active_processes = []
        cli.agent_processes = {}
        cli.runtime_state = {"active_qdiscs": [{"host": "h2", "interface": "h2-eth0"}],
                             "agents": {}, "iperf": [], "downed_links": []}
        cli._last_cleanup_success = True
        with tempfile.TemporaryDirectory() as temp:
            cleanup_file = os.path.join(temp, "cleanup.log")
            with patch.object(faults, "CLEANUP_LOGFILE", cleanup_file), \
                    patch.object(faults.time, "time", side_effect=[123.456, 123.789]):
                cli.do_fault("clear")
            with open(cleanup_file, encoding="utf-8") as f:
                lines = f.read().splitlines()
        self.assertEqual(lines[0], "123.456 QDISC_REMOVED host=h2 interface=h2-eth0")
        self.assertEqual(lines[1], "123.789 FAULT_CLEARED")

    def test_emergency_cleanup_uses_manifest_and_records_verified_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            cleanup_path = os.path.join(temp, "cleanup.log")
            runner = test_runner.MininetSubprocessRunner(
                "topo.py", temp, outdir=temp, cleanup_log_path=cleanup_path)
            runner.process_group_id = 3456
            state = {"run_token": runner.run_token, "process_group_id": 3456,
                     "host_pids": {"h2": 4001}, "agents": {}, "iperf": [],
                     "active_qdiscs": [{"host": "h2", "interface": "h2-eth0",
                                        "host_pid": 4001}]}
            with open(runner.runtime_state_path, "w", encoding="utf-8") as f:
                json.dump(state, f)
            results = [types.SimpleNamespace(returncode=1, stdout=""),
                       types.SimpleNamespace(returncode=0, stdout="qdisc noqueue\n")]
            with patch.object(runner, "_owned_host_pid", return_value=True), \
                    patch.object(test_runner.subprocess, "run", side_effect=results), \
                    patch.object(test_runner.time, "time", return_value=124.5):
                self.assertTrue(runner.emergency_cleanup())
            with open(cleanup_path, encoding="utf-8") as f:
                self.assertEqual(f.read().strip(),
                    "124.500 QDISC_REMOVED host=h2 interface=h2-eth0 method=out_of_band")

    def test_stalled_cli_runs_scoped_cleanup_before_topology_group_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            cleanup_path = os.path.join(temp, "cleanup.log")
            runner = test_runner.MininetSubprocessRunner(
                "topo.py", temp, outdir=temp, cleanup_log_path=cleanup_path)
            runner.proc = FakeRunnerProcess(runner, respond=False)
            runner.command_channel_healthy = False
            runner.process_group_id = 9898
            runner.stdout_lines = ["MININET_NET_STOPPED"]
            runner.emergency_cleanup = Mock(return_value=True)
            runner._finalize_namespace_cleanup = Mock(return_value=True)

            def terminate(proc, pgid):
                proc.returncode = 0
                return True

            with patch.object(test_runner, "terminate_owned_process_group", side_effect=terminate), \
                    patch.object(test_runner.os, "killpg", side_effect=ProcessLookupError):
                self.assertTrue(runner.stop())
            runner.emergency_cleanup.assert_called_once_with()

    def test_unrelated_process_group_is_never_signaled(self):
        runner = test_runner.MininetSubprocessRunner("topo.py", ".",
                                                       outdir=tempfile.gettempdir())
        runner.process_group_id = 9000
        with patch.object(test_runner.os, "getpgid", return_value=9001), \
                patch.object(test_runner.os, "kill") as kill:
            runner._terminate_manifest_process({"pid": 77}, "iperf")
        kill.assert_not_called()

    def test_agent_cleanup_signals_only_exact_run_owned_pid(self):
        runner = test_runner.MininetSubprocessRunner("topo.py", ".",
                                                     outdir=tempfile.gettempdir())
        host = "h2"
        agent_script = os.path.realpath(test_runner.DEFAULT_AGENT_SCRIPT)
        record = {"pid": 7788, "host": host, "run_id": runner.run_token,
                  "host_pid": 778, "pid_scope": "host",
                  "command": agent_script, "start_ticks": 400}
        state = {"run_token": runner.run_token, "host_pids": {host: 778}}
        details = {"argv": ["python3.14", agent_script, "--id=h2",
                            "--run-id=" + runner.run_token],
                   "state": "S", "start_ticks": 400}
        with patch.object(test_runner, "read_host_process_identity",
                          side_effect=[details, None]), \
                patch.object(test_runner, "signal_verified_host_pid") as kill:
            self.assertTrue(runner._terminate_agent_record(host, record, state))
        kill.assert_called_once_with(7788, test_runner.signal.SIGTERM)

        record["run_id"] = "another-run"
        with patch.object(test_runner, "read_host_process_identity", return_value=details), \
                patch.object(test_runner, "signal_verified_host_pid") as kill:
            self.assertFalse(runner._terminate_agent_record(host, record, state))
        kill.assert_not_called()

    def test_recovery_stays_null_without_verified_removal_timestamp(self):
        baseline = [{"rtt_ms": 1.0, "packet_loss_pct": 0.0}]
        samples = [{"phase": "post_clear", "timestamp": 12.0,
                    "rtt_ms": 1.0, "packet_loss_pct": 0.0},
                   {"phase": "post_clear", "timestamp": 13.0,
                    "rtt_ms": 1.0, "packet_loss_pct": 0.0}]
        self.assertIsNone(test_runner.find_service_recovery(samples, 10.0, baseline))


if __name__ == "__main__":
    unittest.main()
