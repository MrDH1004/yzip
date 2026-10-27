"""updater against a local fake of the GitHub releases API (never the real network)."""
import hashlib
import http.server
import json
import os
import tempfile
import threading
import unittest

import updater


class FakeGitHub(http.server.BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):
        body = self.routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class UpdaterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        os.environ["YZIP_UPDATE_URL"] = cls.base + "/latest"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        os.environ.pop("YZIP_UPDATE_URL", None)

    def publish(self, tag, payload=b"MZ fake installer" * 1000, sha=None, assets=True, **extra):
        name = f"yzip-setup-{tag.lstrip('v')}.exe"
        rel = {"tag_name": tag, "html_url": "https://example/release", "body": "notes", **extra,
               "assets": [{"name": name, "browser_download_url": self.base + "/" + name},
                          {"name": name + ".sha256", "browser_download_url": self.base + "/" + name + ".sha256"}]
               if assets else []}
        FakeGitHub.routes = {"/latest": json.dumps(rel).encode(), "/" + name: payload,
                             "/" + name + ".sha256": ((sha or hashlib.sha256(payload).hexdigest()) + f"  {name}\n").encode()}
        return payload

    def test_newer_release_is_found_and_verified(self):
        payload = self.publish("v9.9.9")
        rel = updater.check("1.0.0")
        self.assertEqual(rel.version, "9.9.9")
        seen = []
        path = updater.download(rel, progress=lambda d, t, n: seen.append(d))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), payload)
        self.assertEqual(seen[-1], len(payload))

    def test_same_or_older_is_not_offered(self):
        self.publish("v1.0.0")
        self.assertIsNone(updater.check("1.0.0"))
        self.publish("v0.9.9")
        self.assertIsNone(updater.check("1.0.0"))
        self.publish("v1.0.10")
        self.assertIsNotNone(updater.check("1.0.9"), "numeric, not text comparison")

    def test_tampered_download_is_rejected_and_removed(self):
        self.publish("v2.0.0", sha="0" * 64)
        rel = updater.check("1.0.0")
        before = set(os.listdir(tempfile.gettempdir()))
        with self.assertRaises(ValueError):
            updater.download(rel)
        leftovers = [d for d in set(os.listdir(tempfile.gettempdir())) - before
                     if os.listdir(os.path.join(tempfile.gettempdir(), d))]
        self.assertEqual(leftovers, [], "no unverified installer left on disk")

    def test_release_without_installer_or_prerelease_is_ignored(self):
        self.publish("v3.0.0", assets=False)
        self.assertIsNone(updater.check("1.0.0"))
        self.publish("v3.0.0", prerelease=True)
        self.assertIsNone(updater.check("1.0.0"))

    def test_due_respects_setting_and_test_switch(self):
        os.environ.pop("YZIP_NO_UPDATE", None)
        self.assertTrue(updater.due({}))
        self.assertFalse(updater.due({"auto_update": False}))
        import time
        self.assertTrue(updater.due({"update_checked_at": time.time() - 60}))
        os.environ["YZIP_NO_UPDATE"] = "1"
        self.assertFalse(updater.due({}))
        os.environ.pop("YZIP_NO_UPDATE")


if __name__ == "__main__":
    unittest.main()
