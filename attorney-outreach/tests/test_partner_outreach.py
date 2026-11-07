"""Run: python3 -m unittest discover -s tests  (from attorney-outreach/)"""
import contextlib, csv, io, os, sys, tempfile, unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import partner_outreach as po  # noqa: E402

SENDER = ["--sender-name", "Jon Broder", "--sender-email", "jbroder@vortexlegal.com"]


def run(*args):
    out = io.StringIO()
    with mock.patch.object(sys, "argv", ["po", *args]), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(io.StringIO()):
        code = po.main()
    return code, out.getvalue()


def rows(p):
    return list(csv.DictReader(open(p))) if p.exists() else []


class PartnerOutreachTest(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        code, _ = run("prepare", "--state", str(self.state), *SENDER, "--contacts",
                      str(ROOT / "examples" / "crm_partners.csv"), "--sender-company", "Vortex Legal",
                      "--sender-address", "123 Main St")
        self.assertEqual(code, 0)

    def test_partner_filter_and_suppression(self):
        queued = {r["to"] for r in rows(self.state / "queue.csv")}
        self.assertEqual(queued, {"dana.whitfield@example.com", "marcus.o@example.com",
                                  "ana.ruiz@example.com", "lee.park@example.com"})

    def test_title_rules(self):
        self.assertTrue(po.is_partner("Managing Partner"))
        self.assertTrue(po.is_partner("Shareholder"))
        self.assertFalse(po.is_partner("Associate Partner"))
        self.assertFalse(po.is_partner("Partnerships Manager"))
        self.assertFalse(po.is_partner("Senior Associate"))

    def test_dry_run_changes_nothing(self):
        before = (self.state / "queue.csv").read_text()
        code, out = run("send", "--state", str(self.state), *SENDER)
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertEqual((self.state / "queue.csv").read_text(), before)
        self.assertFalse((self.state / "sent_log.csv").exists())

    def test_live_send_respects_cap_and_suppression(self):
        run("suppress", "--state", str(self.state), "marcus.o@example.com")
        sent = []
        with mock.patch.dict(os.environ, {"SENDGRID_API_KEY": "x"}), \
                mock.patch.object(po, "send_sendgrid", side_effect=lambda a, r: (sent.append(r["to"]) or (True, "202"))), \
                mock.patch.object(po.time, "sleep"):
            code, _ = run("send", "--state", str(self.state), *SENDER, "--live", "--ignore-hours", "--daily-cap", "2")
            self.assertEqual(code, 0)
            self.assertEqual(len(sent), 2)
            self.assertNotIn("marcus.o@example.com", sent)
            # Cap reached: a second run the same day sends nothing.
            run("send", "--state", str(self.state), *SENDER, "--live", "--ignore-hours", "--daily-cap", "2")
            self.assertEqual(len(sent), 2)
        self.assertEqual(len(rows(self.state / "queue.csv")), 1)
        # Re-preparing doesn't re-queue people already emailed.
        run("prepare", "--state", str(self.state), *SENDER, "--contacts",
            str(ROOT / "examples" / "crm_partners.csv"), "--sender-company", "V", "--sender-address", "X")
        self.assertTrue(set(sent).isdisjoint(r["to"] for r in rows(self.state / "queue.csv")))

    def test_refuses_without_credentials_or_blocked_sender(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            code, _ = run("send", "--state", str(self.state), *SENDER, "--live", "--ignore-hours")
        self.assertEqual(code, 2)
        code, _ = run("send", "--state", str(self.state), "--sender-name", "x", "--sender-email", "a@prividential.ai")
        self.assertEqual(code, 2)

    def test_business_hours(self):
        sat = datetime(2026, 11, 7, 11, 0, tzinfo=po.ET)
        with mock.patch.dict(os.environ, {"SENDGRID_API_KEY": "x"}), mock.patch.object(po, "datetime") as dt:
            dt.now.side_effect = lambda tz=None: sat
            dt.fromisoformat = datetime.fromisoformat
            code, out = run("send", "--state", str(self.state), *SENDER, "--live")
        self.assertEqual(code, 0)
        self.assertIn("Outside sending window", out)

    def test_subject_rule(self):
        self.assertEqual(po.fix_subject("I’m house roles in NYC"), "In-house roles in NYC")


if __name__ == "__main__":
    unittest.main()
