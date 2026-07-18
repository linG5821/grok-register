"""浏览器启停：单次尝试、代理探活、可中断主线程调用、停止清理。"""
import queue
import threading
import time
import unittest
from unittest.mock import patch

import proxy_manager
import registration_browser as rb


class ProbeProxyEndpointTests(unittest.TestCase):
    def test_probe_empty_url_returns_true(self):
        self.assertTrue(proxy_manager.probe_proxy_endpoint(""))

    def test_probe_unreachable_port_returns_false(self):
        # 1 是保留端口，连不上
        self.assertFalse(
            proxy_manager.probe_proxy_endpoint("http://127.0.0.1:1", timeout=0.3)
        )

    def test_probe_open_local_port_returns_true(self):
        import socket

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(
                proxy_manager.probe_proxy_endpoint(
                    f"http://127.0.0.1:{port}", timeout=1.0
                )
            )
        finally:
            srv.close()


class StartBrowserSingleAttemptTests(unittest.TestCase):
    def tearDown(self):
        rb.browser = None
        rb.page = None
        rb.browser_proxy_bridge = None
        rb.browser_started_with_proxy = False

    def test_max_attempts_one_does_not_retry_chromium(self):
        calls = {"n": 0}

        def boom(*_a, **_k):
            calls["n"] += 1
            raise RuntimeError("chrome fail")

        # registration_browser 依赖 bind_runtime 注入这些名字
        rb.prepare_browser_proxy = lambda **k: ("", None)
        rb.create_browser_options = lambda **k: object()
        rb.get_configured_proxy = lambda: ""
        with patch.object(rb, "Chromium", side_effect=boom), \
             patch.object(rb, "_cleanup_orphaned_debug_browser"), \
             patch.object(rb.time, "sleep", return_value=None):
            with self.assertRaises(Exception) as ctx:
                rb.start_browser(log_callback=None, use_proxy=False, max_attempts=1)
        self.assertEqual(calls["n"], 1)
        self.assertIn("已重试1次", str(ctx.exception))

    def test_preflight_fail_skips_chromium_and_rotates(self):
        chromes = {"n": 0}
        prep_calls = {"n": 0}
        rotates = []
        marks = []

        def fake_chrome(*_a, **_k):
            chromes["n"] += 1
            raise AssertionError("should not launch chrome when probe fails")

        def prep(**_k):
            prep_calls["n"] += 1
            return ("http://127.0.0.1:9", None)

        rb.get_configured_proxy = lambda: "http://127.0.0.1:1"
        rb.prepare_browser_proxy = prep
        rb.create_browser_options = lambda **k: object()
        with patch("proxy_manager.probe_proxy_usable", return_value=False) as probe, \
             patch("proxy_manager.mark_proxy_dead", side_effect=lambda *a, **k: marks.append(1)), \
             patch("proxy_manager.proxy_health_should_run", return_value=False), \
             patch("proxy_manager.rotate_session", side_effect=lambda reason="": rotates.append(reason) or "abc"), \
             patch.object(rb, "Chromium", side_effect=fake_chrome), \
             patch.object(rb.time, "sleep", return_value=None):
            with self.assertRaises(Exception) as ctx:
                rb.start_browser(log_callback=None, use_proxy=True, max_attempts=1)
        self.assertEqual(chromes["n"], 0)
        self.assertEqual(prep_calls["n"], 0)
        self.assertTrue(probe.called)
        self.assertTrue(marks)
        self.assertEqual(rotates, ["proxy-preflight-fail"])
        self.assertIn("代理", str(ctx.exception))


class StopBrowserQuitTimeoutTests(unittest.TestCase):
    def tearDown(self):
        rb.browser = None
        rb.page = None
        rb.browser_proxy_bridge = None

    def test_stop_browser_passes_timeout_and_force(self):
        class FakeBrowser:
            def __init__(self):
                self.kwargs = None

            def quit(self, **kwargs):
                self.kwargs = kwargs

        fake = FakeBrowser()
        rb.browser = fake
        rb.stop_browser()
        self.assertIsNone(rb.browser)
        self.assertIsNotNone(fake.kwargs)
        self.assertIn("timeout", fake.kwargs)
        self.assertTrue(fake.kwargs.get("force", False) or fake.kwargs.get("timeout", 0) > 0)


class CallOnUiThreadInterruptTests(unittest.TestCase):
    """不依赖完整 GUI，测可复用的 wait/cancel 逻辑。"""

    def test_wait_respects_cancel_callback(self):
        from grok_register_ttk import _wait_for_ui_result

        done = queue.Queue()
        cancelled = {"v": False}

        def cancel():
            return cancelled["v"]

        def blocker():
            # 主线程永远不 put，靠 cancel 退出
            time.sleep(10)

        result = {"err": None}

        def worker():
            try:
                _wait_for_ui_result(done, timeout=5, cancel_callback=cancel)
            except Exception as exc:
                result["err"] = exc

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.05)
        cancelled["v"] = True
        t.join(timeout=2)
        self.assertFalse(t.is_alive())
        from grok_register_ttk import RegistrationCancelled
        self.assertIsInstance(result["err"], RegistrationCancelled)

    def test_wait_timeout_raises(self):
        from grok_register_ttk import _wait_for_ui_result

        done = queue.Queue()
        with self.assertRaises(TimeoutError):
            _wait_for_ui_result(done, timeout=0.15, cancel_callback=None)


class BrowserRetryHelperTests(unittest.TestCase):
    def test_retry_wrapper_retries_then_succeeds(self):
        from grok_register_ttk import _retry_ui_browser_op

        calls = {"n": 0}
        logs = []

        def op():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("fail")
            return "ok"

        def call_ui(fn, timeout=90, cancel_callback=None):
            return fn()

        result = _retry_ui_browser_op(
            name="启动浏览器",
            single_shot=op,
            call_on_ui=call_ui,
            should_stop=lambda: False,
            log=logs.append,
            max_attempts=4,
            per_attempt_timeout=1,
            sleep_fn=lambda s: None,
            cleanup_fn=lambda: None,
        )
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)

    def test_retry_wrapper_stops_on_cancel(self):
        from grok_register_ttk import _retry_ui_browser_op, RegistrationCancelled

        with self.assertRaises(RegistrationCancelled):
            _retry_ui_browser_op(
                name="启动浏览器",
                single_shot=lambda: (_ for _ in ()).throw(RuntimeError("x")),
                call_on_ui=lambda fn, timeout=90, cancel_callback=None: fn(),
                should_stop=lambda: True,
                log=lambda m: None,
                max_attempts=4,
                per_attempt_timeout=1,
                sleep_fn=lambda s: None,
                cleanup_fn=lambda: None,
            )


if __name__ == "__main__":
    unittest.main()
