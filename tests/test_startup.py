"""Safety regressions with no real application or process operations."""

import argparse
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cdp_discovery as discovery
import hanhua_v2 as hh
import shouhu


def arguments(**overrides):
    defaults = dict(port=None, force_restart=False, yes=False, install_dir=None, no_launch=False)
    return argparse.Namespace(**(defaults | overrides))


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.find = self.enterContext(patch.object(discovery, "find_running_instance"))
        self.pids = self.enterContext(patch.object(discovery, "antigravity_pids", return_value=set()))
        self.stop = self.enterContext(patch.object(discovery, "stop_confirmed_instance"))
        self.free = self.enterContext(patch.object(discovery, "require_free_port"))
        self.exe = self.enterContext(patch.object(hh, "find_antigravity_exe", return_value="Antigravity.exe"))
        self.launch = self.enterContext(patch.object(hh, "launch_antigravity", return_value=object()))
        self.confirm = self.enterContext(patch.object(hh, "_confirm_restart", return_value=False))
        self.find.return_value = discovery.Instance(set())

    def test_reuse_without_installation_path_and_without_restart(self):
        page = {"id": "fixture"}
        self.find.return_value = discovery.Instance({23}, discovery.Endpoint("127.0.0.1", 7542), page)
        self.exe.return_value = None
        self.assertEqual(hh.prepare_instance(arguments()), (7542, page))
        self.exe.assert_not_called()
        self.launch.assert_not_called()
        self.stop.assert_not_called()

    def test_loading_instance_waits_instead_of_starting(self):
        self.find.return_value = discovery.Instance({23}, discovery.Endpoint("127.0.0.1", 7542))
        self.assertEqual(hh.prepare_instance(arguments()), (7542, None))
        self.launch.assert_not_called()
        self.stop.assert_not_called()

    def test_running_without_cdp_never_starts_or_stops_by_default(self):
        self.find.return_value = discovery.Instance({23})
        for options in ({}, {"no_launch": True}, {"port": 9334}):
            with self.subTest(options=options), self.assertRaises(discovery.DiscoveryError):
                hh.prepare_instance(arguments(**options))
        self.launch.assert_not_called()
        self.stop.assert_not_called()

    def test_absent_no_launch_and_discovery_failure_are_safe(self):
        with self.assertRaises(discovery.DiscoveryError):
            hh.prepare_instance(arguments(no_launch=True))
        self.find.side_effect = discovery.DiscoveryError("unknown")
        with self.assertRaises(discovery.DiscoveryError):
            hh.prepare_instance(arguments())
        self.launch.assert_not_called()
        self.stop.assert_not_called()

    def test_new_launch_checks_port_and_process_race(self):
        self.assertEqual(hh.prepare_instance(arguments(port=9334)), (9334, None))
        self.free.assert_called_once_with(9334)
        self.launch.assert_called_once_with("Antigravity.exe", 9334)
        self.launch.reset_mock()
        self.pids.return_value = {23}
        with self.assertRaises(discovery.DiscoveryError):
            hh.prepare_instance(arguments())
        self.launch.assert_not_called()

    def test_occupied_port_and_launch_failure_are_errors(self):
        self.free.side_effect = discovery.DiscoveryError("occupied")
        with self.assertRaises(discovery.DiscoveryError):
            hh.prepare_instance(arguments())
        self.launch.assert_not_called()
        self.free.side_effect = None
        self.launch.return_value = None
        with self.assertRaises(discovery.DiscoveryError):
            hh.prepare_instance(arguments())

    def test_restart_requires_confirmation_and_an_executable(self):
        self.find.return_value = discovery.Instance({23})
        with self.assertRaises(discovery.DiscoveryError):
            hh.prepare_instance(arguments(force_restart=True))
        self.confirm.assert_called_once_with(False)
        self.stop.assert_not_called()
        self.launch.assert_not_called()
        self.confirm.reset_mock()
        self.exe.return_value = None
        with self.assertRaises(discovery.DiscoveryError):
            hh.prepare_instance(arguments(force_restart=True, yes=True))
        self.confirm.assert_not_called()
        self.stop.assert_not_called()

    def test_confirmed_restart_failure_never_launches(self):
        self.find.return_value = discovery.Instance({23})
        self.confirm.return_value = True
        self.stop.side_effect = discovery.DiscoveryError("still alive")
        with self.assertRaises(discovery.DiscoveryError):
            hh.prepare_instance(arguments(force_restart=True, yes=True))
        self.stop.assert_called_once_with({23})
        self.launch.assert_not_called()

    def test_restart_checks_unrelated_target_port_before_termination(self):
        self.find.return_value = discovery.Instance({23}, discovery.Endpoint("127.0.0.1", 7542))
        self.confirm.return_value = True
        self.free.side_effect = discovery.DiscoveryError("occupied")
        with self.assertRaises(discovery.DiscoveryError):
            hh.prepare_instance(arguments(force_restart=True, yes=True))
        self.stop.assert_not_called()
        self.confirm.assert_not_called()
        self.launch.assert_not_called()

    def test_confirmed_restart_stops_only_the_snapshot_then_launches(self):
        self.find.return_value = discovery.Instance({23, 24})
        self.confirm.return_value = True
        self.assertEqual(hh.prepare_instance(arguments(force_restart=True, yes=True)), (9333, None))
        self.confirm.assert_called_once_with(True)
        self.stop.assert_called_once_with({23, 24})
        self.launch.assert_called_once_with("Antigravity.exe", 9333)

    def test_cli_rejects_conflicting_or_invalid_options_before_discovery(self):
        for options in (["--yes"], ["--force-restart", "--no-launch"],
                        ["--force-restart", "--autostart", "on"], ["--port", "0"], ["--port", "65536"]):
            with self.subTest(options=options), patch.object(sys, "argv", ["hanhua_v2.py", *options]):
                with self.assertRaises(SystemExit) as result:
                    hh.main()
                self.assertEqual(result.exception.code, 2)
        self.find.assert_not_called()

    def test_wait_revalidates_and_propagates_loss_of_identity(self):
        self.find.side_effect = [discovery.Instance({23}), discovery.DiscoveryError("ownership changed")]
        with patch.object(hh.time, "sleep"), self.assertRaises(discovery.DiscoveryError):
            hh.wait_for_debug_port(7542)
        self.assertEqual(self.find.call_args_list[0].args, (7542,))
        self.assertEqual(self.find.call_count, 2)

    def test_guard_never_starts_or_stops_on_absent_unreachable_or_failed_discovery(self):
        states = [discovery.Instance(set()), discovery.Instance({23}),
                  discovery.Instance({23}, discovery.Endpoint("127.0.0.1", 7542)),
                  discovery.DiscoveryError("unknown")]
        for state in states:
            self.find.side_effect = state if isinstance(state, Exception) else None
            self.find.return_value = state
            with self.subTest(state=state), patch.object(shouhu, "log"), patch.object(
                    shouhu.time, "sleep", side_effect=KeyboardInterrupt), patch.object(hh, "cdp_inject") as inject:
                shouhu.watch()
                inject.assert_not_called()
        self.stop.assert_not_called()
        self.launch.assert_not_called()

    def test_guard_reuses_discovered_page(self):
        page = {"id": "fixture"}
        self.find.return_value = discovery.Instance({23}, discovery.Endpoint("127.0.0.1", 7542), page)
        with patch.object(shouhu, "log"), patch.object(shouhu.time, "sleep", side_effect=KeyboardInterrupt), patch.object(
                shouhu, "engine_alive", return_value=False), patch.object(hh, "cdp_inject", return_value={}) as inject:
            shouhu.watch()
            self.assertEqual(inject.call_args.args[0], page)
        self.launch.assert_not_called()
        self.stop.assert_not_called()


class ConfirmationTests(unittest.TestCase):
    def test_confirmation_is_explicit_and_noninteractive_is_safe(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(sys.stdin, "isatty", return_value=False), patch(
                "builtins.input") as prompt:
            self.assertFalse(hh._confirm_restart(False))
            prompt.assert_not_called()
            self.assertTrue(hh._confirm_restart(True))
        for answer, accepted in [("RESTART", True), ("yes", False), ("", False)]:
            with contextlib.redirect_stdout(io.StringIO()), patch.object(sys.stdin, "isatty", return_value=True), patch(
                    "builtins.input", return_value=answer):
                self.assertEqual(hh._confirm_restart(False), accepted)
        with contextlib.redirect_stdout(io.StringIO()), patch.object(sys.stdin, "isatty", return_value=True), patch(
                "builtins.input", side_effect=EOFError):
            self.assertFalse(hh._confirm_restart(False))

    def test_launchers_delegate_without_killing_and_preserve_failure(self):
        batch = (ROOT / "qidong.bat").read_text(encoding="ascii")
        mac = (ROOT / "qidong_mac.sh").read_text(encoding="utf-8")
        self.assertNotIn("taskkill", batch.lower())
        self.assertNotIn("killall", mac)
        self.assertIn('set "RESULT=%ERRORLEVEL%"', batch)
        self.assertIn("exit /b %RESULT%", batch)
        self.assertIn('exit "$result"', mac)


if __name__ == "__main__":
    unittest.main()
