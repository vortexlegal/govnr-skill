"""Run: python3 -m unittest discover -s tests  (from attorney-outreach/)"""
import csv, io, json, contextlib, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import crm_sync  # noqa: E402

ITEMS = [
    {"id": "a1", "first_name": "Dana", "last_name": "W", "personal_email": "dana@gmail.com", "work_email": "d@firm.com",
     "current_title": "Partner", "current_company": "Harlow LLP", "practice_areas": ["LITIGATION"], "is_dnu": False,
     "verification_status": "ok", "verified_at": "2026-11-07T00:00:00Z", "updated_at": "2026-11-07T00:00:00Z"},
    {"id": "a2", "first_name": "Ray", "last_name": "M", "personal_email": "ray@gmail.com", "current_title": "",
     "inferred_title": "Associate", "inferred_title_source": "is currently an associate at Kucker",
     "current_company": "Kucker Marino", "is_dnu": True, "dnu_reason": "Do not recruit", "verification_status": "ok"},
    {"id": "a3", "first_name": "Sam", "last_name": "L", "personal_email": "sam@gmail.com", "current_title": None,
     "current_company": "Lee & Park LLP", "is_dnu": False, "verification_status": "ok"},
]


class Fake(BaseHTTPRequestHandler):
    calls = []
    def log_message(self, *a): pass
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        Fake.calls.append((self.headers.get("x-api-key"), q))
        if self.headers.get("x-api-key") != "k":
            self.send_response(401); self.end_headers(); return
        if "since" in q:
            page = {"items": [dict(ITEMS[2], current_title="Associate")], "next_cursor": None, "generated_at": "2026-11-09T10:00:00Z"}
        elif q.get("cursor") == ["2"]:
            page = {"items": ITEMS[2:], "next_cursor": None, "generated_at": "2026-11-08T10:00:00Z"}
        else:
            page = {"items": ITEMS[:2], "next_cursor": "2", "generated_at": "2026-11-08T10:00:00Z"}
        body = json.dumps(page).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)


def rows(p):
    with open(p) as f:
        return list(csv.DictReader(f))


class CrmSyncTest(unittest.TestCase):
    def setUp(self):
        self.srv = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        self.out = Path(tempfile.mkdtemp())
        Fake.calls = []

    def tearDown(self):
        self.srv.shutdown()

    def run_sync(self, *extra, key="k"):
        with mock.patch.dict("os.environ", {"CRM_EXPORT_API_KEY": key, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}), \
                mock.patch.object(sys, "argv", ["s", "--out", str(self.out), "--base-url", self.base, *extra]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = crm_sync.main()
        return code, out.getvalue()

    def test_full_then_incremental(self):
        code, out = self.run_sync()
        self.assertEqual(code, 0)
        self.assertIn("3 new", out)
        self.assertEqual([r["id"] for r in rows(self.out / "titled.csv")], ["a1", "a2"])
        self.assertEqual(rows(self.out / "titled.csv")[1]["current_title"], "Associate")
        self.assertIn("CRM notes", rows(self.out / "titled.csv")[1]["title_source"])
        self.assertEqual([r["id"] for r in rows(self.out / "untitled.csv")], ["a3"])
        self.assertEqual(rows(self.out / "titled.csv")[0]["practice_areas"], "LITIGATION")
        self.assertEqual([r["email"] for r in rows(self.out / "crm_dnu.csv")], ["ray@gmail.com"])
        # second run asks only for changes, a few hours before the last export
        code, out = self.run_sync()
        self.assertIn("since", Fake.calls[-1][1])
        self.assertTrue(Fake.calls[-1][1]["since"][0].startswith("2026-11-08T04:00"))
        self.assertIn("0 new candidate(s), 1 updated", out)
        self.assertEqual(rows(self.out / "untitled.csv"), [])
        self.assertEqual(len(rows(self.out / "candidates.csv")), 3)

    def test_bad_key_stops(self):
        with self.assertRaises(SystemExit) as e:
            self.run_sync(key="wrong")
        self.assertIn("refused the API key", str(e.exception))


if __name__ == "__main__":
    unittest.main()
