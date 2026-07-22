"""代理健康缓存：探活、等待、dead、无代理 no-op。"""
import threading
import time
import unittest
from unittest.mock import patch

import app_config
import proxy_manager as pm


class ProxyHealthTests(unittest.TestCase):
    def setUp(self):
        self._prev = dict(app_config.config)
        self._prev_persist = pm.persist_dead_pool_removals
        app_config.config.clear()
        app_config.config.update(app_config.DEFAULT_CONFIG)
        # 默认不写真实 config.json
        pm.persist_dead_pool_removals = False
        pm.reset_proxy_health_state()
        pm._current_session_id = ""
        pm._current_pool_url = ""
        pm._current_pool_index = -1
        pm._last_expanded = ""

    def tearDown(self):
        pm.reset_proxy_health_state()
        pm.persist_dead_pool_removals = self._prev_persist
        app_config.config.clear()
        app_config.config.update(self._prev)

    def test_should_run_false_without_proxy(self):
        app_config.config["proxy"] = ""
        app_config.config["proxy_pool"] = []
        self.assertFalse(pm.proxy_health_should_run())
        pm.start_background_scan()
        self.assertEqual(pm.scan_status()["scanning"], False)
        self.assertEqual(pm.wait_until_available(), "")

    def test_should_run_true_with_pool(self):
        app_config.config["proxy_pool"] = ["http://127.0.0.1:1"]
        self.assertTrue(pm.proxy_health_should_run())

    def test_wait_raises_when_all_dead(self):
        app_config.config["proxy_pool"] = ["http://127.0.0.1:1", "http://127.0.0.1:2"]
        with patch.object(pm, "probe_proxy_usable", return_value=False):
            pm.start_background_scan(concurrency=2)
            # 等扫完
            for _ in range(50):
                if pm.scan_status()["full_pass_done"] and not pm.scan_status()["scanning"]:
                    break
                time.sleep(0.05)
            with self.assertRaises(pm.NoAvailableProxyError):
                pm.wait_until_available(poll=0.05)

    def test_wait_succeeds_when_one_alive(self):
        app_config.config["proxy_pool"] = [
            "http://127.0.0.1:1",
            "http://good.example:9999",
        ]

        def probe(url, tcp_timeout=None, http_timeout=None):
            return "good.example" in str(url)

        with patch.object(pm, "probe_proxy_usable", side_effect=probe):
            pm.start_background_scan(concurrency=2)
            url = pm.wait_until_available(poll=0.05)
            self.assertIn("good.example", url)
            self.assertGreaterEqual(pm.available_count(), 1)

    def test_probe_usable_requires_tcp_and_grok(self):
        with patch.object(pm, "probe_proxy_endpoint", return_value=True), \
             patch.object(pm, "probe_proxy_grok_access", return_value=False):
            self.assertFalse(pm.probe_proxy_usable("http://127.0.0.1:1080"))
        with patch.object(pm, "probe_proxy_endpoint", return_value=False), \
             patch.object(pm, "probe_proxy_grok_access", return_value=True) as grok:
            self.assertFalse(pm.probe_proxy_usable("http://127.0.0.1:1080"))
            grok.assert_not_called()
        with patch.object(pm, "probe_proxy_endpoint", return_value=True), \
             patch.object(pm, "probe_proxy_grok_access", return_value=True):
            self.assertTrue(pm.probe_proxy_usable("http://127.0.0.1:1080"))

    def test_probe_grok_accepts_any_http_status(self):
        class Resp:
            status_code = 403

        with patch.object(pm.requests, "get", return_value=Resp()):
            self.assertTrue(
                pm.probe_proxy_grok_access("http://127.0.0.1:9", timeout=1.0)
            )

    def test_probe_grok_fails_on_connection_error(self):
        with patch.object(pm.requests, "get", side_effect=OSError("down")):
            self.assertFalse(
                pm.probe_proxy_grok_access("http://127.0.0.1:9", timeout=1.0)
            )

    def test_mark_dead_removes_from_available(self):
        app_config.config["proxy_pool"] = ["http://a:1", "http://b:2"]
        with pm._health_lock:
            pm._available.append("http://a:1")
            pm._available.append("http://b:2")
        pm.mark_proxy_dead("http://a:1", reason="test")
        with pm._health_lock:
            self.assertNotIn("http://a:1", pm._available)
            self.assertIn("http://a:1", pm._dead)

    def test_remove_dead_from_config_only_deletes(self):
        """dead 只从 pool 删除，不覆盖 available。"""
        app_config.config["proxy_pool"] = ["http://a:1", "http://b:2", "http://c:3"]
        n = pm.remove_dead_from_config({"http://b:2"}, reason="unit")
        self.assertEqual(n, 1)
        self.assertEqual(app_config.config["proxy_pool"], ["http://a:1", "http://c:3"])

    def test_remove_dead_noop_when_missing(self):
        app_config.config["proxy_pool"] = ["http://a:1"]
        n = pm.remove_dead_from_config({"http://nope:9"})
        self.assertEqual(n, 0)
        self.assertEqual(app_config.config["proxy_pool"], ["http://a:1"])

    def test_mark_dead_schedules_pool_removal(self):
        app_config.config["proxy_pool"] = ["http://a:1", "http://b:2"]
        with pm._health_lock:
            pm._available.append("http://a:1")
        pm.mark_proxy_dead("http://a:1", reason="test")
        # debounce 未 flush 前 pool 可能仍在；强制 flush
        n = pm.flush_pending_proxy_removals()
        self.assertEqual(n, 1)
        self.assertEqual(app_config.config["proxy_pool"], ["http://b:2"])

    def test_remove_dead_save_failure_keeps_memory_delete(self):
        app_config.config["proxy_pool"] = ["http://a:1", "http://b:2"]
        pm.persist_dead_pool_removals = True
        with patch.object(app_config, "save_config", side_effect=RuntimeError("disk full")):
            n = pm.remove_dead_from_config(["http://a:1"], reason="fail")
        # save 失败返回 0，但内存 pool 已删（本进程仍跳过）
        self.assertEqual(n, 0)
        self.assertEqual(app_config.config["proxy_pool"], ["http://b:2"])

    def test_health_scan_removes_dead_from_pool(self):
        app_config.config["proxy_pool"] = [
            "http://dead:1",
            "http://good.example:9999",
        ]

        def probe(url, tcp_timeout=None, http_timeout=None):
            return "good.example" in str(url)

        with patch.object(pm, "probe_proxy_usable", side_effect=probe):
            pm.start_background_scan(concurrency=2)
            for _ in range(80):
                if pm.scan_status()["full_pass_done"] and not pm.scan_status()["scanning"]:
                    break
                time.sleep(0.05)
            # 扫完会 flush
            self.assertNotIn("http://dead:1", app_config.config["proxy_pool"])
            self.assertIn("http://good.example:9999", app_config.config["proxy_pool"])

    def test_rotate_prefers_available(self):
        app_config.config["proxy_pool"] = [
            "http://dead:1",
            "http://alive:2",
            "http://other:3",
        ]
        with pm._health_lock:
            pm._available.append("http://alive:2")
        sid = pm.rotate_session("test")
        self.assertTrue(sid)
        self.assertEqual(pm.current_pool_url(), "http://alive:2")

    def test_wait_cancel(self):
        app_config.config["proxy_pool"] = ["http://127.0.0.1:1"]
        # 永不 full_pass：卡住 scanning 模拟
        with pm._health_lock:
            pm._scanning = True
            pm._full_pass_done = False
        cancelled = {"v": False}

        def cancel():
            return cancelled["v"]

        def stopper():
            time.sleep(0.15)
            cancelled["v"] = True

        threading.Thread(target=stopper, daemon=True).start()
        with self.assertRaises(pm.ProxyWaitCancelled):
            pm.wait_until_available(cancel_callback=cancel, poll=0.05)

    def test_rotate_skips_dead_pool_entries(self):
        app_config.config["proxy_pool"] = [
            "http://127.0.0.1:1",
            "http://127.0.0.1:2",
            "http://127.0.0.1:3",
        ]
        with pm._health_lock:
            pm._dead.add("http://127.0.0.1:1")
            pm._dead.add("http://127.0.0.1:2")
            pm._available.append("http://127.0.0.1:3")
        with patch.object(pm, "start_background_scan"):
            pm.rotate_session("skip-dead")
        self.assertEqual(pm.current_pool_url(), "http://127.0.0.1:3")

    def test_rotate_does_not_bind_only_dead(self):
        app_config.config["proxy_pool"] = ["http://127.0.0.1:1", "http://127.0.0.1:2"]
        with pm._health_lock:
            pm._dead.add("http://127.0.0.1:1")
            pm._dead.add("http://127.0.0.1:2")
        pm._current_pool_url = "http://127.0.0.1:1"
        with patch.object(pm, "start_background_scan"):
            pm.rotate_session("all-dead")
        self.assertEqual(pm.current_pool_url(), "")

    def test_acquire_raises_when_empty_not_fallback_dead(self):
        app_config.config["proxy_pool"] = ["http://127.0.0.1:1"]
        with pm._health_lock:
            pm._dead.add("http://127.0.0.1:1")
            pm._full_pass_done = True
            pm._scanning = False
        with patch.object(pm, "start_background_scan"):
            with self.assertRaises(pm.NoAvailableProxyError):
                pm.acquire_healthy_proxy("x")

    def test_acquire_waits_then_gets_available(self):
        app_config.config["proxy_pool"] = ["http://127.0.0.1:9"]
        with pm._health_lock:
            pm._scanning = True
            pm._full_pass_done = False

        def fill_later():
            time.sleep(0.2)
            with pm._health_lock:
                pm._available.append("http://127.0.0.1:9")
                pm._scanning = False
                pm._full_pass_done = True

        threading.Thread(target=fill_later, daemon=True).start()
        with patch.object(pm, "start_background_scan"):
            sid = pm.acquire_healthy_proxy("wait", wait_sec=2.0, poll=0.05)
        self.assertTrue(sid)
        self.assertEqual(pm.current_pool_url(), "http://127.0.0.1:9")

    def test_acquire_wait_timeout_while_scanning(self):
        app_config.config["proxy_pool"] = ["http://127.0.0.1:1"]
        with pm._health_lock:
            pm._scanning = True
            pm._full_pass_done = False
        with patch.object(pm, "start_background_scan"):
            with self.assertRaises(pm.NoAvailableProxyError):
                pm.acquire_healthy_proxy("timeout", wait_sec=0.25, poll=0.05)


if __name__ == "__main__":
    unittest.main()
