"""proxy_manager 单元测试：占位符替换、池选择策略、rotate_session 语义。"""
import os
import unittest

import app_config
import proxy_manager


class ProxyManagerTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("https_proxy", None)
        os.environ.pop("http_proxy", None)
        self._prev = dict(app_config.config)
        app_config.config.clear()
        app_config.config.update(app_config.DEFAULT_CONFIG)
        # 重置 proxy_manager 内部状态
        proxy_manager._current_session_id = ""
        proxy_manager._current_pool_url = ""
        proxy_manager._current_pool_index = -1
        proxy_manager._last_expanded = ""
        self.pm = proxy_manager

    def tearDown(self):
        app_config.config.clear()
        app_config.config.update(self._prev)
        proxy_manager._current_session_id = ""
        proxy_manager._current_pool_url = ""
        proxy_manager._current_pool_index = -1
        proxy_manager._last_expanded = ""

    def _set_config(self, **overrides):
        app_config.config.update(overrides)

    def test_placeholder_expand_stable_within_session(self):
        self._set_config(proxy="http://u-session-{rand}:pw@host:7000")
        self.pm.rotate_session("t1")
        first = self.pm.expand_proxy(app_config.config["proxy"])
        second = self.pm.expand_proxy(app_config.config["proxy"])
        self.assertEqual(first, second)
        self.assertIn(self.pm.current_session_id(), first)

    def test_rotate_changes_expansion(self):
        self._set_config(proxy="http://u-{rand}:pw@host:7000")
        self.pm.rotate_session("first")
        first = self.pm.expand_proxy(app_config.config["proxy"])
        self.pm.rotate_session("second")
        second = self.pm.expand_proxy(app_config.config["proxy"])
        self.assertNotEqual(first, second)

    def test_pool_round_robin_advances_each_rotate(self):
        self._set_config(
            proxy="",
            proxy_pool=[
                "http://a:1",
                "socks5://b:2",
                "http://c:3",
            ],
            proxy_pool_strategy="round_robin",
        )
        picks = []
        for _ in range(6):
            self.pm.rotate_session("loop")
            picks.append(self.pm.expand_proxy(""))
        self.assertEqual(picks[0], "http://a:1")
        self.assertEqual(picks[1], "socks5://b:2")
        self.assertEqual(picks[2], "http://c:3")
        self.assertEqual(picks[3], "http://a:1")
        self.assertEqual(picks[4], "socks5://b:2")
        self.assertEqual(picks[5], "http://c:3")

    def test_pool_random_touches_all_eventually(self):
        self._set_config(
            proxy="",
            proxy_pool=["http://a:1", "http://b:2", "http://c:3"],
            proxy_pool_strategy="random",
        )
        seen = set()
        for _ in range(200):
            self.pm.rotate_session("loop")
            seen.add(self.pm.expand_proxy(""))
        self.assertEqual(seen, {"http://a:1", "http://b:2", "http://c:3"})

    def test_pool_sticky_stays_on_first_pick(self):
        self._set_config(
            proxy="",
            proxy_pool=["http://a:1", "http://b:2"],
            proxy_pool_strategy="sticky",
        )
        self.pm.rotate_session("first")
        first = self.pm.expand_proxy("")
        for _ in range(5):
            self.pm.rotate_session("more")
            self.assertEqual(self.pm.expand_proxy(""), first)

    def test_pool_ignores_proxy_field(self):
        self._set_config(
            proxy="http://fallback:9",
            proxy_pool=["http://in-pool:1"],
            proxy_pool_strategy="round_robin",
        )
        self.pm.rotate_session("go")
        self.assertEqual(self.pm.expand_proxy(app_config.config["proxy"]), "http://in-pool:1")

    def test_pool_entry_supports_placeholder(self):
        self._set_config(
            proxy="",
            proxy_pool=["http://u-{rand}:pw@host:1"],
            proxy_pool_strategy="round_robin",
        )
        self.pm.rotate_session("go")
        expanded = self.pm.expand_proxy("")
        self.assertIn(self.pm.current_session_id(), expanded)
        self.assertNotIn("{rand}", expanded)

    def test_empty_proxy_clears_environment(self):
        os.environ["HTTPS_PROXY"] = "http://stale:1"
        os.environ["HTTP_PROXY"] = "http://stale:1"
        self._set_config(proxy="", proxy_pool=[])
        self.pm.rotate_session("clear")
        self.assertNotIn("HTTPS_PROXY", os.environ)
        self.assertNotIn("HTTP_PROXY", os.environ)

    def test_expand_returns_empty_when_no_proxy(self):
        self._set_config(proxy="", proxy_pool=[])
        self.assertEqual(self.pm.expand_proxy(""), "")
        self.assertEqual(self.pm.expand_proxy("   "), "")


if __name__ == "__main__":
    unittest.main()
