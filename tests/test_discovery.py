"""Offline safety tests for process-owned CDP discovery and explicit restart."""

import pathlib
import subprocess
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cdp_discovery as discovery


def completed(stdout=b"", returncode=0, stderr=b""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def page(**overrides):
    value = {
        "id": "main",
        "type": "page",
        "title": "Antigravity",
        "url": "https://127.0.0.1:7555/",
        "webSocketDebuggerUrl": "ws://127.0.0.1:7542/devtools/page/main",
    }
    value.update(overrides)
    return value


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(discovery.sys, "platform", "win32"))
        self.enterContext(mock.patch.object(discovery.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True))
        self.enterContext(mock.patch.object(discovery.socket, "SO_EXCLUSIVEADDRUSE", -5, create=True))
        self.run = self.enterContext(mock.patch.object(discovery.subprocess, "run"))
        self.run.side_effect = AssertionError("Unmocked process invocation")
        self.http = self.enterContext(mock.patch.object(discovery.http.client, "HTTPConnection"))
        self.http.side_effect = AssertionError("Unmocked HTTP connection")
        self.socket = self.enterContext(mock.patch.object(discovery.socket, "socket"))
        self.socket.side_effect = AssertionError("Unmocked socket")
        self.sleep = self.enterContext(mock.patch.object(discovery.time, "sleep"))

    def command_result(self, result):
        self.run.side_effect = None
        self.run.return_value = result

    def test_windows_pid_table_matches_full_executable_name(self):
        self.command_result(completed(
            b'"System","4","Services","0","1 K"\r\n'
            b'"Antigravity.exe","9132","Console","1","1,024 K"\r\n'
            b'"ANTIGRAVITY.EXE","20060","\xbf\xd8\xd6\xc6\xcc\xa8","1","4 K"\r\n'
            b'"not-antigravity.exe","99","Console","1","1 K"\r\n'
            b'"chrome.exe","913","Console","1","1 K"\r\n'
        ))
        self.assertEqual(discovery.antigravity_pids(), {9132, 20060})
        self.assertEqual(self.run.call_args.args[0], ["tasklist", "/FO", "CSV", "/NH"])
        self.assertEqual(self.run.call_args.kwargs["creationflags"], 0x08000000)

    def test_windows_valid_table_without_antigravity_is_empty(self):
        self.command_result(completed(b'"System","4","Services","0","1 K"\r\n'))
        self.assertEqual(discovery.antigravity_pids(), set())

    def test_windows_unreadable_or_failed_pid_table_is_not_empty_success(self):
        for result in (
            completed(),
            completed(b"Access is denied.\r\n", 1),
            completed(b"unexpected output\r\n"),
            completed(b'"Antigravity.exe","not-a-pid"\r\n'),
        ):
            with self.subTest(result=result):
                self.command_result(result)
                with self.assertRaises(discovery.DiscoveryError):
                    discovery.antigravity_pids()

    def test_process_invocation_errors_fail_closed(self):
        for error in (PermissionError("denied"), subprocess.TimeoutExpired("tasklist", 5)):
            with self.subTest(error=type(error).__name__):
                self.run.side_effect = error
                with self.assertRaises(discovery.DiscoveryError):
                    discovery.antigravity_pids()

    def test_macos_pgrep_distinguishes_absent_and_unknown(self):
        with mock.patch.object(discovery.sys, "platform", "darwin"):
            self.command_result(completed(b"9132\n20060\n"))
            self.assertEqual(discovery.antigravity_pids(), {9132, 20060})
            self.assertEqual(self.run.call_args.args[0], ["pgrep", "-x", "Antigravity"])
            self.assertNotIn("creationflags", self.run.call_args.kwargs)
            self.command_result(completed(returncode=1))
            self.assertEqual(discovery.antigravity_pids(), set())
            for result in (completed(returncode=2), completed(), completed(b"0\n"), completed(b"-1\n"), completed(b"9132 oops")):
                with self.subTest(result=result):
                    self.command_result(result)
                    with self.assertRaises(discovery.DiscoveryError):
                        discovery.antigravity_pids()

    def test_unsupported_platform_fails_without_commands(self):
        with mock.patch.object(discovery.sys, "platform", "other"):
            with self.assertRaises(discovery.DiscoveryError):
                discovery.antigravity_pids()
        self.run.assert_not_called()

    def test_windows_endpoints_require_owned_loopback_tcp_listener(self):
        self.command_result(completed(b"\n".join([
            b"TCP 127.0.0.1:7542 0.0.0.0:0 LISTENING 9132",
            b"TCP [::1]:7542 [::]:0 LISTENING 9132",
            b"TCP 127.0.0.1:7542 0.0.0.0:0 LISTENING 9132",
            b"TCP 127.0.0.1:9333 0.0.0.0:0 LISTENING 99",
            b"TCP 127.0.0.1:9334 127.0.0.1:80 ESTABLISHED 9132",
            b"TCP 0.0.0.0:9335 0.0.0.0:0 LISTENING 9132",
            b"TCP [::]:9336 [::]:0 LISTENING 9132",
            b"TCP 192.168.1.10:9337 0.0.0.0:0 LISTENING 9132",
            b"TCP 127.0.0.1:0 0.0.0.0:0 LISTENING 9132",
            b"TCP 127.0.0.1:65536 0.0.0.0:0 LISTENING 9132",
            b"TCP 127.0.0.1:not-a-port 0.0.0.0:0 LISTENING 9132",
            b"UDP 127.0.0.1:9338 *:* 9132",
        ])))
        self.assertEqual(discovery.listening_endpoints({9132}), [
            discovery.Endpoint("127.0.0.1", 7542), discovery.Endpoint("::1", 7542),
        ])

    def test_listener_errors_fail_closed_and_no_pids_skip_commands(self):
        self.assertEqual(discovery.listening_endpoints(set()), [])
        self.run.assert_not_called()
        self.command_result(completed(returncode=1))
        with self.assertRaises(discovery.DiscoveryError):
            discovery.listening_endpoints({9132})

    def test_macos_lsof_requires_confirmed_owner(self):
        with mock.patch.object(discovery.sys, "platform", "darwin"):
            self.command_result(completed(
                b"p9132\nn127.0.0.1:7542\nn[::1]:7542\nn*:9333\n"
                b"p99\nn127.0.0.1:9334\npbad\nn127.0.0.1:9335\n"
                b"p9132\nn192.168.1.10:9336\n"
            ))
            self.assertEqual(discovery.listening_endpoints({9132}), [
                discovery.Endpoint("127.0.0.1", 7542), discovery.Endpoint("::1", 7542),
            ])
            self.assertEqual(self.run.call_args.args[0], [
                "lsof", "-nP", "-a", "-p", "9132", "-iTCP", "-sTCP:LISTEN", "-Fpn",
            ])
            self.command_result(completed(returncode=1))
            self.assertEqual(discovery.listening_endpoints({9132}), [])
            self.command_result(completed(returncode=1, stderr=b"permission denied"))
            with self.assertRaises(discovery.DiscoveryError):
                discovery.listening_endpoints({9132})

    def test_http_accepts_only_bounded_valid_200_json_and_always_closes(self):
        self.http.side_effect = None
        connection = self.http.return_value
        response = connection.getresponse.return_value
        endpoint = discovery.Endpoint("127.0.0.1", 7542)
        for status, body, expected in (
            (200, b'{"User-Agent":"Antigravity/2.29.0"}', {"User-Agent": "Antigravity/2.29.0"}),
            (404, b'{"User-Agent":"Antigravity/2.29.0"}', None),
            (302, b"{}", None),
            (200, b"<html>not JSON</html>", None),
            (200, b"x" * (1024 * 1024 + 1), None),
        ):
            with self.subTest(status=status, size=len(body)):
                connection.reset_mock()
                response.status = status
                response.read.return_value = body
                self.assertEqual(discovery._http_json(endpoint, "/json/version"), expected)
                connection.request.assert_called_once_with("GET", "/json/version")
                connection.close.assert_called_once()
        connection.request.side_effect = OSError("connection refused")
        self.assertIsNone(discovery._http_json(endpoint, "/json/version"))
        self.assertEqual(connection.close.call_count, 2)
        self.http.assert_called_with("127.0.0.1", 7542, timeout=0.5)

    def test_candidates_reject_unsafe_urls_types_and_mismatched_target_ids(self):
        endpoint = discovery.Endpoint("127.0.0.1", 7542)
        rejected = [
            None,
            {},
            page(type="service_worker"),
            page(url="data:text/html,loading"),
            page(url="https://example.com/"),
            page(url="https://127.0.0.1/"),
            page(url="https://test-user:test-secret@127.0.0.1:7555/"),
            page(id=None),
            page(id=""),
            page(id=123),
            page(id="other"),
            page(webSocketDebuggerUrl="wss://127.0.0.1:7542/devtools/page/main"),
            page(webSocketDebuggerUrl="ws://example.com:7542/devtools/page/main"),
            page(webSocketDebuggerUrl="ws://user@127.0.0.1:7542/devtools/page/main"),
            page(webSocketDebuggerUrl="ws://127.0.0.1:9999/devtools/page/main"),
            page(webSocketDebuggerUrl="ws://127.0.0.1:7542/devtools/browser/main"),
            page(webSocketDebuggerUrl="ws://127.0.0.1:7542/devtools/page/main/extra"),
            page(webSocketDebuggerUrl="ws://127.0.0.1:7542/devtools/page/other"),
            page(webSocketDebuggerUrl="ws://127.0.0.1:bad/devtools/page/main"),
            page(webSocketDebuggerUrl=None),
        ]
        for invalid in rejected:
            with self.subTest(invalid=invalid):
                self.assertEqual(discovery.candidate_pages(endpoint, [invalid]), [])
        self.assertEqual(discovery.candidate_pages(endpoint, {"pages": [page()]}), [])
        self.assertEqual(discovery.candidate_pages(endpoint, rejected + [page()]), [page()])

    def test_page_websocket_is_pinned_to_verified_ipv4_or_ipv6_listener(self):
        original = page(webSocketDebuggerUrl="ws://localhost:7542/devtools/page/main?x=1#ignored")
        for host, netloc in (("127.0.0.1", "127.0.0.1"), ("::1", "[::1]")):
            with self.subTest(host=host):
                candidates = discovery.candidate_pages(discovery.Endpoint(host, 7542), [original])
                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0]["webSocketDebuggerUrl"], f"ws://{netloc}:7542/devtools/page/main?x=1")
        self.assertEqual(original["webSocketDebuggerUrl"], "ws://localhost:7542/devtools/page/main?x=1#ignored")

    def test_candidates_are_not_main_window_acceptance_and_titles_are_not_identity(self):
        endpoint = discovery.Endpoint("127.0.0.1", 7542)
        candidates = [
            page(id="preview", title="Antigravity", url="http://127.0.0.1:3000/",
                 webSocketDebuggerUrl="ws://127.0.0.1:7542/devtools/page/preview"),
            page(title="A renamed conversation"),
            page(id="loading", title="Loading",
                 webSocketDebuggerUrl="ws://127.0.0.1:7542/devtools/page/loading"),
        ]
        # Every local candidate still requires cdp_targets' in-page identity guard.
        self.assertEqual(discovery.candidate_pages(endpoint, candidates), candidates)
        self.assertTrue(all("confirmed" not in candidate for candidate in candidates))

    def test_multiple_candidates_deduplicate_by_valid_target_id(self):
        endpoint = discovery.Endpoint("127.0.0.1", 7542)
        second = page(id="second", webSocketDebuggerUrl="ws://127.0.0.1:7542/devtools/page/second")
        candidates = [
            page(webSocketDebuggerUrl="ws://127.0.0.1:7542/devtools/page/wrong"),
            page(),
            page(title="duplicate main", webSocketDebuggerUrl="ws://localhost:7542/devtools/page/main"),
            second,
            dict(second),
        ]
        self.assertEqual(discovery.candidate_pages(endpoint, candidates), [page(), second])

    def test_instance_keeps_page_compatibility_without_shared_lists(self):
        endpoint = discovery.Endpoint("127.0.0.1", 7542)
        old = discovery.Instance({9132}, endpoint, page())
        self.assertEqual(old.pages, [page()])
        pages = [page(), page(id="second")]
        multiple = discovery.Instance({9132}, endpoint, pages=pages)
        self.assertEqual(multiple.page, pages[0])
        self.assertEqual(multiple.pages, pages)
        first = discovery.Instance(set())
        second = discovery.Instance(set())
        first.pages.append(page())
        self.assertEqual(second.pages, [])

    def mock_discovery(self, endpoints, responses):
        pids = self.enterContext(mock.patch.object(discovery, "antigravity_pids", return_value={9132}))
        listeners = self.enterContext(mock.patch.object(discovery, "listening_endpoints", return_value=endpoints))
        request = self.enterContext(mock.patch.object(discovery, "_http_json", side_effect=lambda endpoint, path: responses.get((endpoint.port, path))))
        return pids, listeners, request

    def test_random_port_reused_only_with_antigravity_user_agent(self):
        endpoint = discovery.Endpoint("127.0.0.1", 7542)
        pids, listeners, request = self.mock_discovery([endpoint], {
            (7542, "/json/version"): {"User-Agent": "Mozilla/5.0 Antigravity/2.29.0 Electron/44.5.1"},
            (7542, "/json"): [page()],
        })
        instance = discovery.find_running_instance()
        self.assertEqual(instance, discovery.Instance({9132}, endpoint, page(), version="2.29.0"))
        pids.assert_called_once_with()
        listeners.assert_called_once_with({9132})
        self.assertEqual(request.call_count, 2)

    def test_find_returns_all_safe_candidates_for_identity_checks(self):
        endpoint = discovery.Endpoint("127.0.0.1", 7542)
        preview = page(id="preview", title="Local preview", url="http://127.0.0.1:3000/",
                       webSocketDebuggerUrl="ws://127.0.0.1:7542/devtools/page/preview")
        loading = page(id="loading", title="Loading",
                       webSocketDebuggerUrl="ws://127.0.0.1:7542/devtools/page/loading")
        self.mock_discovery([endpoint], {
            (7542, "/json/version"): {"User-Agent": "Antigravity/2.29.0"},
            (7542, "/json"): [preview, page(), loading, page(url="https://example.com/"), page()],
        })
        instance = discovery.find_running_instance()
        self.assertEqual(instance.pages, [preview, page(), loading])
        self.assertEqual(instance.page, preview)
        self.assertEqual(instance.endpoint, endpoint)

    def test_invalid_version_responses_are_not_cdp_matches(self):
        endpoint = discovery.Endpoint("127.0.0.1", 7542)
        _, _, request = self.mock_discovery([endpoint], {})
        for response in (
            None, [], {}, {"User-Agent": None}, {"User-Agent": 123},
            {"User-Agent": "Chrome/152.0.0.0"},
            {"User-Agent": "FakeAntigravity/2.29.0"},
            {"User-Agent": "Antigravity/not-a-version"},
            {"User-Agent": "Antigravity/2.29not-real"},
        ):
            with self.subTest(response=response):
                request.side_effect = None
                request.return_value = response
                request.reset_mock()
                self.assertEqual(discovery.find_running_instance(), discovery.Instance({9132}))
                request.assert_called_once_with(endpoint, "/json/version")

    def test_running_cdp_without_safe_candidates_is_not_absent(self):
        endpoint = discovery.Endpoint("127.0.0.1", 7542)
        self.mock_discovery([endpoint], {
            (7542, "/json/version"): {"User-Agent": "Antigravity/2.29.0"},
            (7542, "/json"): [page(url="data:text/html,loading")],
        })
        self.assertEqual(discovery.find_running_instance(), discovery.Instance({9132}, endpoint, version="2.29.0"))

    def test_target_list_failure_is_not_an_empty_window_snapshot(self):
        endpoint = discovery.Endpoint("127.0.0.1", 7542)
        _, _, request = self.mock_discovery([endpoint], {})
        for bad_list in (None, {}, False, "private response"):
            with self.subTest(response=bad_list):
                request.side_effect = lambda endpoint, path: (
                    {"User-Agent": "Antigravity/2.29.0"} if path == "/json/version" else bad_list)
                with self.assertRaises(discovery.DiscoveryError) as error:
                    discovery.find_running_instance()
                self.assertEqual(error.exception.reason, "target-list-unavailable")
                self.assertNotIn("private", str(error.exception))

    def test_explicit_port_never_falls_back_to_another_valid_listener(self):
        endpoints = [discovery.Endpoint("127.0.0.1", 7542), discovery.Endpoint("127.0.0.1", 9333)]
        _, _, request = self.mock_discovery(endpoints, {
            (7542, "/json/version"): {"User-Agent": "Antigravity/2.29.0"},
            (7542, "/json"): [page()],
            (9333, "/json/version"): {"User-Agent": "Chrome/152.0.0.0"},
        })
        self.assertEqual(discovery.find_running_instance(9333), discovery.Instance({9132}))
        request.assert_called_once_with(endpoints[1], "/json/version")
        request.reset_mock()
        self.assertEqual(discovery.find_running_instance(12345), discovery.Instance({9132}))
        request.assert_not_called()

    def test_multiple_cdp_ports_require_explicit_choice_but_dual_stack_is_one(self):
        endpoints = [
            discovery.Endpoint("127.0.0.1", 7542), discovery.Endpoint("::1", 7542),
            discovery.Endpoint("127.0.0.1", 9333),
        ]
        _, listeners, _ = self.mock_discovery(endpoints, {
            (7542, "/json/version"): {"User-Agent": "Antigravity/2.29.0"},
            (7542, "/json"): [page()],
            (9333, "/json/version"): {"User-Agent": "Antigravity/2.29.0"},
            (9333, "/json"): [page(id="other", webSocketDebuggerUrl="ws://127.0.0.1:9333/devtools/page/other")],
        })
        with self.assertRaisesRegex(discovery.DiscoveryError, "--port"):
            discovery.find_running_instance()
        self.assertEqual(discovery.find_running_instance(9333).endpoint.port, 9333)
        listeners.return_value = endpoints[:2]
        self.assertEqual(discovery.find_running_instance().endpoint, endpoints[0])

    def test_find_propagates_discovery_failure_without_http(self):
        with mock.patch.object(discovery, "antigravity_pids", side_effect=discovery.DiscoveryError("unknown")):
            with self.assertRaises(discovery.DiscoveryError):
                discovery.find_running_instance()
        self.http.assert_not_called()

    def test_free_port_uses_exclusive_loopback_bind_and_rejects_occupied(self):
        self.socket.side_effect = None
        sock = self.socket.return_value.__enter__.return_value
        discovery.require_free_port(9333)
        sock.bind.assert_called_once_with(("127.0.0.1", 9333))
        sock.setsockopt.assert_called_once_with(discovery.socket.SOL_SOCKET, -5, 1)
        self.socket.return_value.__exit__.assert_called_once()
        sock.bind.side_effect = OSError("address in use")
        with self.assertRaises(discovery.DiscoveryError):
            discovery.require_free_port(9333)

    def test_restart_refuses_unconfirmed_new_pids_before_termination(self):
        with mock.patch.object(discovery, "antigravity_pids", return_value={9132, 20060}):
            with self.assertRaisesRegex(discovery.DiscoveryError, "changed"):
                discovery.stop_confirmed_instance({9132})
        self.run.assert_not_called()

    def test_restart_of_already_exited_instance_does_not_terminate_anything(self):
        with mock.patch.object(discovery, "antigravity_pids", return_value=set()):
            discovery.stop_confirmed_instance({9132})
        self.run.assert_not_called()

    def test_restart_targets_only_still_present_confirmed_pids(self):
        for platform, expected in (
            ("win32", ["taskkill", "/F", "/PID", "9132"]),
            ("darwin", ["kill", "-TERM", "9132"]),
        ):
            with self.subTest(platform=platform), mock.patch.object(discovery.sys, "platform", platform):
                self.command_result(completed())
                self.run.reset_mock()
                with mock.patch.object(discovery, "antigravity_pids", side_effect=[{9132}, set()]) as pids:
                    with mock.patch.object(discovery.time, "monotonic", side_effect=[0, 0.1]):
                        discovery.stop_confirmed_instance({9132, 20060})
                self.assertEqual(self.run.call_args.args[0], expected)
                self.assertEqual(pids.call_count, 2)
                self.run.assert_called_once()

    def test_restart_rejects_residual_or_new_processes_without_killing_more(self):
        for remaining in ({9132}, {20060}):
            with self.subTest(remaining=remaining):
                self.command_result(completed())
                self.run.reset_mock()
                with mock.patch.object(discovery, "antigravity_pids", side_effect=[{9132}, remaining]):
                    with mock.patch.object(discovery.time, "monotonic", side_effect=[0, 0.1, 5.1]):
                        with self.assertRaisesRegex(discovery.DiscoveryError, "still running"):
                            discovery.stop_confirmed_instance({9132})
                self.run.assert_called_once()
                self.assertEqual(self.run.call_args.args[0], ["taskkill", "/F", "/PID", "9132"])

    def test_restart_process_inspection_errors_propagate(self):
        with mock.patch.object(discovery, "antigravity_pids", side_effect=discovery.DiscoveryError("unknown")):
            with self.assertRaises(discovery.DiscoveryError):
                discovery.stop_confirmed_instance({9132})
        self.run.assert_not_called()
        self.command_result(completed())
        with mock.patch.object(discovery, "antigravity_pids", side_effect=[{9132}, discovery.DiscoveryError("unknown")]):
            with mock.patch.object(discovery.time, "monotonic", side_effect=[0, 0.1]):
                with self.assertRaises(discovery.DiscoveryError):
                    discovery.stop_confirmed_instance({9132})
        self.run.assert_called_once()

    def test_failed_termination_cannot_succeed_while_process_remains(self):
        self.command_result(completed(returncode=1, stderr=b"access denied"))
        with mock.patch.object(discovery, "antigravity_pids", return_value={9132}):
            with mock.patch.object(discovery.time, "monotonic", side_effect=[0, 0.1, 5.1]):
                with self.assertRaises(discovery.DiscoveryError):
                    discovery.stop_confirmed_instance({9132})
        self.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
