"""open_signup_page：代理失败时换节点，不再静默直连。"""
import unittest
from unittest.mock import patch

import registration_browser as rb


class _Wait:
    def doc_loaded(self, *a, **k):
        return None


class DummyPage:
    def __init__(self, url="https://accounts.x.ai/sign-up", proxy_error=False):
        self.url = url
        self._proxy_error = proxy_error
        self.html = "proxy error" if proxy_error else "ok"
        self.wait = _Wait()

    def get(self, url, timeout=None, **kwargs):
        self.url = url


class DummyBrowser:
    def __init__(self, pages):
        self.pages = pages
        self.idx = 0

    def get_tab(self, _i=0):
        return self.pages[min(self.idx, len(self.pages) - 1)]

    def new_tab(self, url=None):
        p = self.get_tab()
        if url:
            p.url = url
        return p


class OpenSignupProxyRotateTests(unittest.TestCase):
    def tearDown(self):
        rb.browser = None
        rb.page = None
        rb.browser_started_with_proxy = False
        rb.browser_proxy_bridge = None

    def test_proxy_error_rotates_and_retries_without_direct(self):
        logs = []
        pages = [
            DummyPage(proxy_error=True),
            DummyPage(proxy_error=True),
            DummyPage(proxy_error=False),
        ]
        browser = DummyBrowser(pages)
        rb.browser = browser
        rb.page = pages[0]
        rb.browser_started_with_proxy = True
        restarts = []

        def restart(log_callback=None, use_proxy=True):
            restarts.append(use_proxy)
            browser.idx += 1
            rb.browser_started_with_proxy = bool(use_proxy)
            rb.page = browser.get_tab()
            return rb.browser, rb.page

        with patch.object(rb, "raise_if_cancelled", create=True), \
                patch.object(rb, "sleep_with_cancel", create=True), \
                patch.object(rb, "click_email_signup_button"), \
                patch.object(rb, "get_configured_proxy", create=True, return_value="http://p"), \
                patch.object(rb, "page_has_proxy_error", create=True, side_effect=lambda p: bool(p._proxy_error)), \
                patch.object(rb, "page_has_navigation_failure", create=True, side_effect=lambda p: bool(p._proxy_error)), \
                patch.object(rb, "_signup_proxy_max_attempts", return_value=3), \
                patch.object(rb, "restart_browser", side_effect=restart), \
                patch("proxy_manager.proxy_health_should_run", return_value=False), \
                patch("proxy_manager.rotate_session", return_value="sess12345678") as rot:
            rb.open_signup_page(log_callback=logs.append)

        self.assertTrue(restarts)
        self.assertTrue(all(use is True for use in restarts))
        self.assertGreaterEqual(rot.call_count, 1)
        self.assertTrue(any("换节点" in line for line in logs))
        self.assertFalse(any("回退直连" in line for line in logs))

    def test_proxy_exhausted_raises_not_direct(self):
        logs = []
        pages = [DummyPage(proxy_error=True), DummyPage(proxy_error=True)]
        browser = DummyBrowser(pages)
        rb.browser = browser
        rb.page = pages[0]
        rb.browser_started_with_proxy = True

        def restart(log_callback=None, use_proxy=True):
            browser.idx = min(browser.idx + 1, len(pages) - 1)
            rb.browser_started_with_proxy = True
            rb.page = browser.get_tab()
            return rb.browser, rb.page

        with patch.object(rb, "raise_if_cancelled", create=True), \
                patch.object(rb, "sleep_with_cancel", create=True), \
                patch.object(rb, "click_email_signup_button"), \
                patch.object(rb, "get_configured_proxy", create=True, return_value="http://p"), \
                patch.object(rb, "page_has_proxy_error", create=True, return_value=True), \
                patch.object(rb, "page_has_navigation_failure", create=True, return_value=True), \
                patch.object(rb, "_signup_proxy_max_attempts", return_value=2), \
                patch.object(rb, "restart_browser", side_effect=restart), \
                patch("proxy_manager.proxy_health_should_run", return_value=False), \
                patch("proxy_manager.rotate_session", return_value="sess12345678"):
            with self.assertRaises(RuntimeError) as ctx:
                rb.open_signup_page(log_callback=logs.append)
        self.assertIn("换节点重试", str(ctx.exception))
        self.assertFalse(any("回退直连" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
