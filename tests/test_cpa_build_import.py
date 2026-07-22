"""CPA 导出同步产出 grok2api Build 导入文件。"""
import base64
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone

import cpa_build_import as cbi


def _make_jwt(payload: dict) -> str:
    def _b64(obj) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    return f"{_b64({'alg': 'none'})}.{_b64(payload)}.sig"


class BuildImportEntryTests(unittest.TestCase):
    def test_field_mapping(self):
        access = _make_jwt({"sub": "user-123", "team_id": "team-abc"})
        payload = {
            "access_token": access,
            "refresh_token": "rt_1",
            "id_token": "id_1",
            "email": "A@X.com",
            "sub": "user-123",
            "expires_in": 3600,
            "expired": "2026-07-22T10:00:00Z",
        }
        entry = cbi.build_import_entry(payload)
        self.assertEqual(entry["provider"], "grok_build")
        self.assertEqual(entry["name"], "a@x.com")
        self.assertEqual(entry["email"], "a@x.com")
        self.assertEqual(entry["client_id"], cbi.CLIENT_ID)
        self.assertEqual(entry["access_token"], access)
        self.assertEqual(entry["refresh_token"], "rt_1")
        self.assertEqual(entry["id_token"], "id_1")
        self.assertEqual(entry["expires_at"], "2026-07-22T10:00:00Z")
        self.assertEqual(entry["expires_in"], 3600)
        self.assertEqual(entry["user_id"], "user-123")
        self.assertEqual(entry["principal_id"], "user-123")
        self.assertEqual(entry["team_id"], "team-abc")

    def test_expires_at_computed_from_expires_in(self):
        payload = {
            "access_token": "x.y.z",
            "refresh_token": "rt",
            "email": "b@x.com",
            "expires_in": 100,
        }
        entry = cbi.build_import_entry(payload)
        self.assertTrue(entry["expires_at"])  # 现算出来的
        # 应接近现在 + 100s
        parsed = datetime.strptime(entry["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        delta = parsed.timestamp() - time.time()
        self.assertGreater(delta, 50)
        self.assertLess(delta, 150)

    def test_team_id_and_sub_from_jwt_when_missing(self):
        access = _make_jwt({"sub": "sub-from-jwt", "team_id": "tt"})
        payload = {"access_token": access, "refresh_token": "rt", "email": "c@x.com"}
        entry = cbi.build_import_entry(payload)
        self.assertEqual(entry["user_id"], "sub-from-jwt")
        self.assertEqual(entry["team_id"], "tt")


class AppendBuildImportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cbi-test-")
        self.path = os.path.join(self.dir, "grok2api_build_import.json")

    def tearDown(self):
        for name in os.listdir(self.dir):
            try:
                os.unlink(os.path.join(self.dir, name))
            except OSError:
                pass
        os.rmdir(self.dir)

    def _entry(self, email: str) -> dict:
        return {"provider": "grok_build", "name": email, "email": email,
                "access_token": "a", "refresh_token": "r"}

    def test_create_new_file(self):
        cbi.append_build_import(self.path, self._entry("a@x.com"))
        with open(self.path, "r", encoding="utf-8") as h:
            data = json.load(h)
        self.assertEqual(len(data["accounts"]), 1)
        self.assertEqual(data["accounts"][0]["email"], "a@x.com")

    def test_dedup_same_email_overwrites(self):
        cbi.append_build_import(self.path, self._entry("a@x.com"))
        e2 = self._entry("a@x.com")
        e2["access_token"] = "NEW"
        cbi.append_build_import(self.path, e2)
        with open(self.path, "r", encoding="utf-8") as h:
            data = json.load(h)
        self.assertEqual(len(data["accounts"]), 1)
        self.assertEqual(data["accounts"][0]["access_token"], "NEW")

    def test_append_different_emails(self):
        cbi.append_build_import(self.path, self._entry("a@x.com"))
        cbi.append_build_import(self.path, self._entry("b@x.com"))
        with open(self.path, "r", encoding="utf-8") as h:
            data = json.load(h)
        self.assertEqual(len(data["accounts"]), 2)

    def test_corrupted_file_backed_up_and_rebuilt(self):
        with open(self.path, "w", encoding="utf-8") as h:
            h.write("{ this is not valid json ]]]")
        cbi.append_build_import(self.path, self._entry("a@x.com"))
        # 备份存在
        self.assertTrue(os.path.exists(self.path + ".bak"))
        # 目标重建为合法单条
        with open(self.path, "r", encoding="utf-8") as h:
            data = json.load(h)
        self.assertEqual(len(data["accounts"]), 1)
        self.assertEqual(data["accounts"][0]["email"], "a@x.com")

    def test_empty_path_noop(self):
        cbi.append_build_import("", self._entry("a@x.com"))  # 不抛异常即可


class CpaExportIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cbi-int-")
        self.auth_dir = os.path.join(self.dir, "auths")
        os.makedirs(self.auth_dir, exist_ok=True)
        self.import_file = os.path.join(self.dir, "build_import.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_mint_success_writes_import(self):
        import cpa_export

        cpa_json = os.path.join(self.auth_dir, "xai-user.json")
        access = _make_jwt({"sub": "s1", "team_id": "t1"})
        with open(cpa_json, "w", encoding="utf-8") as h:
            json.dump({
                "access_token": access, "refresh_token": "rt",
                "id_token": "id", "email": "user@x.com", "sub": "s1",
                "expires_in": 3600, "expired": "2026-07-22T10:00:00Z",
            }, h)

        def fake_mint(**kwargs):
            return {"ok": True, "email": "user@x.com", "path": cpa_json}

        config = {
            "cpa_export_enabled": True,
            "cpa_auth_dir": self.auth_dir,
            "cpa_build_import_file": self.import_file,
            "cpa_force_standalone": True,
            "cpa_mint_cookie_inject": False,
        }
        from unittest.mock import patch
        with patch.object(cpa_export, "_load_mint_and_export", return_value=fake_mint):
            result = cpa_export.export_cpa_xai_for_account(
                "user@x.com", "pw", config=config
            )
        self.assertTrue(result.get("ok"))
        with open(self.import_file, "r", encoding="utf-8") as h:
            data = json.load(h)
        self.assertEqual(len(data["accounts"]), 1)
        acc = data["accounts"][0]
        self.assertEqual(acc["provider"], "grok_build")
        self.assertEqual(acc["email"], "user@x.com")
        self.assertEqual(acc["team_id"], "t1")

    def test_mint_failure_does_not_write_import(self):
        import cpa_export

        def fake_mint(**kwargs):
            return {"ok": False, "email": "user@x.com", "error": "boom"}

        config = {
            "cpa_export_enabled": True,
            "cpa_auth_dir": self.auth_dir,
            "cpa_build_import_file": self.import_file,
            "cpa_force_standalone": True,
            "cpa_mint_cookie_inject": False,
        }
        from unittest.mock import patch
        with patch.object(cpa_export, "_load_mint_and_export", return_value=fake_mint):
            cpa_export.export_cpa_xai_for_account("user@x.com", "pw", config=config)
        self.assertFalse(os.path.exists(self.import_file))


if __name__ == "__main__":
    unittest.main()
