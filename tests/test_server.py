import ast
import gc
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "server")))
import server
from server import COOKIE_NAME, SECRET_ACCESS_COOKIE, Database, StandaloneHTTPHandler


class TestSyncProfilePluginSyntax(unittest.TestCase):
    def test_plugin_metadata_and_ast(self):
        plugin_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "sync_profile.py"))
        self.assertTrue(os.path.exists(plugin_path))

        with open(plugin_path, "r", encoding="utf-8") as f:
            code = f.read()

        tree = ast.parse(code, filename="sync_profile.py")
        self.assertIsNotNone(tree)

        constants = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if isinstance(node.value, ast.Constant):
                            constants[target.id] = node.value.value
                        elif isinstance(node.value, ast.List):
                            constants[target.id] = [elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)]

        self.assertIn("__id__", constants)
        self.assertEqual(constants["__id__"], "sync_profile")
        self.assertIn("__name__", constants)
        self.assertEqual(constants["__name__"], "SyncProfile")
        self.assertIn("__version__", constants)
        self.assertEqual(constants["__version__"], "11.0.0-beta.1")

    def test_build_peer_color_logic(self):
        def mock_build_peer_color(color_id, bg_emoji_id):
            c = int(color_id) if color_id is not None else 0
            if c < 0:
                c = 0

            bg = 0
            if bg_emoji_id is not None:
                try:
                    bg_str = str(bg_emoji_id).strip()
                    if bg_str and bg_str.isdigit():
                        bg = int(bg_str)
                except Exception:
                    bg = 0

            if c == 0 and bg == 0:
                return None

            class MockTLPeerColor:
                color = 0
                background_emoji_id = 0
                flags = 0

            pc = MockTLPeerColor()
            pc.color = c
            if bg != 0:
                pc.background_emoji_id = bg
                pc.flags = 3
            else:
                pc.flags = 1
            return pc

        pc1 = mock_build_peer_color(11, "5299025466055734222")
        self.assertIsNotNone(pc1)
        self.assertEqual(pc1.color, 11)
        self.assertEqual(pc1.background_emoji_id, 5299025466055734222)
        self.assertEqual(pc1.flags, 3)

        pc2 = mock_build_peer_color(4, 0)
        self.assertIsNotNone(pc2)
        self.assertEqual(pc2.color, 4)
        self.assertEqual(pc2.background_emoji_id, 0)
        self.assertEqual(pc2.flags, 1)

        pc3 = mock_build_peer_color(0, 0)
        self.assertIsNone(pc3)


class TestSyncProfileDatabase(unittest.TestCase):
    def setUp(self):
        self.test_db_path = f"test_sync_{time.time_ns()}.db"
        self.db = Database(self.test_db_path)

    def tearDown(self):
        self.db.close()
        gc.collect()
        for suffix in ("", "-wal", "-shm"):
            path = f"{self.test_db_path}{suffix}"
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

    def test_sessions(self):
        token = self.db.create_session(user_id=12345)
        self.assertIsNotNone(token)
        self.assertTrue(len(token) > 16)

        uid = self.db.validate_session(token)
        self.assertEqual(uid, 12345)

        invalid = self.db.validate_session("wrong_token_1234567890")
        self.assertIsNone(invalid)

    def test_get_all_profiles_and_in_memory_cache(self):
        self.db.upsert_profile({
            "user_id": 111,
            "premium": True,
            "name_color": 2,
            "profile_color": 20,
            "client_type": "AyuGram",
        })
        self.db.upsert_profile({
            "user_id": 222,
            "premium": True,
            "name_color": 4,
            "profile_color": 19,
            "client_type": "exteraGram",
        })
        all_p = self.db.get_all_profiles()
        self.assertEqual(len(all_p), 2)
        self.assertIn("111", all_p)
        self.assertEqual(all_p["111"]["profile_color"], 20)
        self.assertIn("222", all_p)
        self.assertEqual(all_p["222"]["profile_color"], 19)

        batch = self.db.get_profiles_batch([111, 999])
        self.assertEqual(len(batch), 1)
        self.assertIn("111", batch)


class TestSyncProfileServerAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_db_path = f"test_api_sync_{time.time_ns()}.db"
        server.db = Database(cls.test_db_path)

        cls.port = 8899
        cls.server_url = f"http://127.0.0.1:{cls.port}"
        cls.httpd = HTTPServer(("127.0.0.1", cls.port), StandaloneHTTPHandler)
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.db.close()
        gc.collect()
        time.sleep(0.1)
        for suffix in ("", "-wal", "-shm"):
            path = f"{cls.test_db_path}{suffix}"
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

    def test_health_check(self):
        req = urllib.request.Request(f"{self.server_url}/health")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("status"), "ok")

    def test_unauthorized_access_without_cookie(self):
        req = urllib.request.Request(f"{self.server_url}/api/profile/123")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 401)

        req_all = urllib.request.Request(f"{self.server_url}/api/profiles/all")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req_all)
        self.assertEqual(cm.exception.code, 401)

    def test_auth_and_cookie_workflow_and_etag(self):
        cookie_val = f"{COOKIE_NAME}={SECRET_ACCESS_COOKIE}"

        req_profile = urllib.request.Request(
            f"{self.server_url}/api/profile",
            data=json.dumps({
                "user_id": 973400168,
                "premium": True,
                "name_color": 11,
                "name_bg_emoji_id": 5299025466055734222,
                "profile_color": 20,
                "client_type": "AyuGram",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "Cookie": cookie_val},
            method="POST",
        )
        with urllib.request.urlopen(req_profile) as resp:
            self.assertEqual(resp.status, 200)
            saved_data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(saved_data["profile"]["profile_color"], 20)

        req_all = urllib.request.Request(
            f"{self.server_url}/api/profiles/all",
            headers={"Cookie": cookie_val},
        )
        with urllib.request.urlopen(req_all) as resp:
            self.assertEqual(resp.status, 200)
            etag = resp.headers.get("ETag")
            self.assertIsNotNone(etag)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("status"), "ok")
            self.assertIn("973400168", data["profiles"])
            self.assertEqual(data["profiles"]["973400168"]["name_color"], 11)
            self.assertEqual(data["profiles"]["973400168"]["profile_color"], 20)

        req_etag = urllib.request.Request(
            f"{self.server_url}/api/profiles/all",
            headers={"Cookie": cookie_val, "If-None-Match": etag},
        )
        try:
            with urllib.request.urlopen(req_etag) as resp:
                self.assertEqual(resp.status, 304)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 304)

        req_batch = urllib.request.Request(
            f"{self.server_url}/api/profiles/batch",
            data=json.dumps({"user_ids": [973400168]}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Cookie": cookie_val},
            method="POST",
        )
        with urllib.request.urlopen(req_batch) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("973400168", data["profiles"])

    def test_id_spoofing_prevention(self):
        token = server.db.create_session(user_id=1001)
        user_cookie = f"{COOKIE_NAME}={token}"

        req_self = urllib.request.Request(
            f"{self.server_url}/api/profile",
            data=json.dumps({"user_id": 1001, "name_color": 3}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Cookie": user_cookie},
            method="POST",
        )
        with urllib.request.urlopen(req_self) as resp:
            self.assertEqual(resp.status, 200)

        req_spoof = urllib.request.Request(
            f"{self.server_url}/api/profile",
            data=json.dumps({"user_id": 2002, "name_color": 5}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Cookie": user_cookie},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req_spoof)
        self.assertEqual(cm.exception.code, 403)

    def test_status_page_html(self):
        req_html = urllib.request.Request(f"{self.server_url}/")
        with urllib.request.urlopen(req_html) as resp:
            self.assertEqual(resp.status, 200)
            html_text = resp.read().decode("utf-8")
            self.assertIn("SyncProfile Server", html_text)
            self.assertIn("ONLINE", html_text)
            self.assertNotIn("<script>", html_text)

    def test_invalid_inputs(self):
        cookie_val = f"{COOKIE_NAME}={SECRET_ACCESS_COOKIE}"

        req_invalid = urllib.request.Request(
            f"{self.server_url}/api/profile",
            data=json.dumps({"user_id": -5}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Cookie": cookie_val},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req_invalid)
        self.assertEqual(cm.exception.code, 400)

    def test_delta_sync_updates_endpoint(self):
        cookie_val = f"{COOKIE_NAME}={SECRET_ACCESS_COOKIE}"

        req_delta0 = urllib.request.Request(
            f"{self.server_url}/api/profiles/updates?since=0",
            headers={"Cookie": cookie_val},
        )
        with urllib.request.urlopen(req_delta0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("status"), "ok")
            self.assertIn("profiles", data)
            self.assertGreaterEqual(data.get("total", 0), 1)

        now_ts = int(time.time())
        req_save = urllib.request.Request(
            f"{self.server_url}/api/profile",
            data=json.dumps({
                "user_id": 777888,
                "name_color": 5,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "Cookie": cookie_val},
            method="POST",
        )
        with urllib.request.urlopen(req_save) as resp:
            self.assertEqual(resp.status, 200)

        req_delta_recent = urllib.request.Request(
            f"{self.server_url}/api/profiles/updates?since={now_ts - 2}",
            headers={"Cookie": cookie_val},
        )
        with urllib.request.urlopen(req_delta_recent) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("777888", data["profiles"])
            self.assertEqual(data["profiles"]["777888"]["name_color"], 5)

        future_ts = now_ts + 10000
        req_delta_future = urllib.request.Request(
            f"{self.server_url}/api/profiles/updates?since={future_ts}",
            headers={"Cookie": cookie_val},
        )
        try:
            with urllib.request.urlopen(req_delta_future) as resp:
                self.assertEqual(resp.status, 304)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 304)

    def test_backup_endpoint(self):
        cookie_val = f"{COOKIE_NAME}={SECRET_ACCESS_COOKIE}"
        req_backup = urllib.request.Request(
            f"{self.server_url}/api/backup",
            headers={"Cookie": cookie_val},
        )
        with urllib.request.urlopen(req_backup) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("attachment; filename=", resp.headers.get("Content-Disposition", ""))
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("service"), "SyncProfile Backup")
            self.assertIn("profiles", data)
            self.assertIn("stats", data)


if __name__ == "__main__":
    unittest.main()
