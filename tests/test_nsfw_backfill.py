"""NSFW 补开：解析 accounts 文件 + 批量调用 enable_nsfw 链路。"""
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import nsfw_backfill as nb


class NsfwBackfillParseTests(unittest.TestCase):
    def test_parse_standard_line(self):
        rec = nb.parse_accounts_line(
            "a@example.com----Pass1----eyJhbGciOiJIUzI1NiJ9.abc.sig",
            line_no=3,
        )
        self.assertIsNotNone(rec)
        self.assertEqual(rec.email, "a@example.com")
        self.assertEqual(rec.password, "Pass1")
        self.assertEqual(rec.sso, "eyJhbGciOiJIUzI1NiJ9.abc.sig")
        self.assertEqual(rec.line_no, 3)

    def test_parse_sso_prefix(self):
        rec = nb.parse_accounts_line("a@x.com----p----sso=tokenvalue")
        self.assertEqual(rec.sso, "tokenvalue")

    def test_parse_skips_blank_and_comment(self):
        self.assertIsNone(nb.parse_accounts_line(""))
        self.assertIsNone(nb.parse_accounts_line("   "))
        self.assertIsNone(nb.parse_accounts_line("# note"))
        self.assertIsNone(nb.parse_accounts_line("only-two----parts"))

    def test_load_dedupes_same_sso(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".txt") as f:
            f.write("a@x.com----p1----tokA\n")
            f.write("b@x.com----p2----tokA\n")
            f.write("c@x.com----p3----tokB\n")
            f.write("bad-line\n")
            f.write("\n")
            path = f.name
        try:
            records, total, skipped = nb.load_accounts_file(path)
        finally:
            os.unlink(path)
        self.assertEqual(total, 5)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].email, "a@x.com")
        self.assertEqual(records[1].email, "c@x.com")
        self.assertEqual(skipped, 3)


class NsfwBackfillFlowTests(unittest.TestCase):
    def _write(self, lines):
        f = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".txt")
        f.write("\n".join(lines) + "\n")
        f.close()
        return f.name

    def test_backfill_calls_enable_for_each_unique_sso(self):
        path = self._write([
            "a@x.com----p----sso=tok1",
            "b@x.com----p----tok2",
            "c@x.com----p----tok1",  # dedupe
        ])
        calls = []

        def enable(token, log_callback=None):
            calls.append(token)
            return True, "ok"

        logs = []
        try:
            result = nb.backfill_nsfw_from_accounts(
                path,
                enable_nsfw=enable,
                log_callback=logs.append,
                delay_sec=0,
            )
        finally:
            os.unlink(path)

        self.assertEqual(calls, ["tok1", "tok2"])
        self.assertEqual(result.parsed, 2)
        self.assertEqual(result.success, 2)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.skipped, 1)
        self.assertTrue(any("补开结束" in line for line in logs))

    def test_backfill_records_failures(self):
        path = self._write(["ok@x.com----p----good", "bad@x.com----p----badtok"])

        def enable(token, log_callback=None):
            if token == "good":
                return True, "http ok"
            return False, "CF blocked"

        try:
            result = nb.backfill_nsfw_from_accounts(
                path, enable_nsfw=enable, delay_sec=0
            )
        finally:
            os.unlink(path)

        self.assertEqual(result.success, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.failures[0][0], "bad@x.com")
        self.assertIn("CF", result.failures[0][1])

    def test_backfill_respects_cancel(self):
        path = self._write([
            "a@x.com----p----t1",
            "b@x.com----p----t2",
            "c@x.com----p----t3",
        ])
        state = {"n": 0}

        def enable(token, log_callback=None):
            state["n"] += 1
            return True, "ok"

        def cancel():
            return state["n"] >= 1

        try:
            result = nb.backfill_nsfw_from_accounts(
                path,
                enable_nsfw=enable,
                cancel_callback=cancel,
                delay_sec=0,
            )
        finally:
            os.unlink(path)

        self.assertTrue(result.cancelled)
        self.assertEqual(result.success, 1)
        self.assertEqual(state["n"], 1)

    def test_backfill_exception_counts_as_failure(self):
        path = self._write(["a@x.com----p----t1"])

        def enable(token, log_callback=None):
            raise RuntimeError("boom")

        try:
            result = nb.backfill_nsfw_from_accounts(
                path, enable_nsfw=enable, delay_sec=0
            )
        finally:
            os.unlink(path)

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.success, 0)

    def test_dry_run_validate_file(self):
        path = self._write(["a@x.com----p----t1", "nope"])
        try:
            result = nb.dry_run_validate_file(path)
        finally:
            os.unlink(path)
        self.assertEqual(result.parsed, 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.success, 0)

    def test_compatible_with_enable_nsfw_for_token_signature(self):
        """证明与 registration_browser.enable_nsfw_for_token 形参兼容。"""
        path = self._write(["a@x.com----p----sso=realjwt"])
        mock_enable = Mock(return_value=(True, "成功开启 NSFW (HTTP)"))

        def wrapper(token, log_callback=None):
            # 与真实函数相同调用方式
            return mock_enable(token, cf_clearance="", log_callback=log_callback)

        try:
            result = nb.backfill_nsfw_from_accounts(
                path, enable_nsfw=wrapper, delay_sec=0
            )
        finally:
            os.unlink(path)

        self.assertTrue(result.success)
        mock_enable.assert_called_once()
        args, kwargs = mock_enable.call_args
        self.assertEqual(args[0], "realjwt")

    def test_wires_registration_browser_enable_nsfw_http_path(self):
        """集成：backfill → enable_nsfw_for_token → HTTP 回退（无 page）。"""
        import registration_browser as rb

        path = self._write(["a@x.com----p----sso=jwt-integration"])
        rb.page = None  # 强制 HTTP 路径

        with patch.object(rb, "_enable_nsfw_http", return_value=(True, "成功开启 NSFW (HTTP)")) as http_mock:
            try:
                result = nb.backfill_nsfw_from_accounts(
                    path,
                    enable_nsfw=rb.enable_nsfw_for_token,
                    delay_sec=0,
                )
            finally:
                os.unlink(path)

        self.assertEqual(result.success, 1)
        http_mock.assert_called_once()
        self.assertEqual(http_mock.call_args[0][0], "jwt-integration")


if __name__ == "__main__":
    unittest.main()
