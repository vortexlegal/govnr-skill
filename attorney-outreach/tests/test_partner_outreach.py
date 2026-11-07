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
    if not p.exists():
        return []
    with open(p) as f:
        return list(csv.DictReader(f))


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
            code, _ = run("send", "--state", str(self.state), *SENDER, "--transport", "sendgrid", "--live", "--ignore-hours", "--daily-cap", "2")
            self.assertEqual(code, 0)
            self.assertEqual(len(sent), 2)
            self.assertNotIn("marcus.o@example.com", sent)
            # Cap reached: a second run the same day sends nothing.
            run("send", "--state", str(self.state), *SENDER, "--transport", "sendgrid", "--live", "--ignore-hours", "--daily-cap", "2")
            self.assertEqual(len(sent), 2)
        self.assertEqual(len(rows(self.state / "queue.csv")), 1)
        # Re-preparing doesn't re-queue people already emailed.
        run("prepare", "--state", str(self.state), *SENDER, "--contacts",
            str(ROOT / "examples" / "crm_partners.csv"), "--sender-company", "V", "--sender-address", "X")
        self.assertTrue(set(sent).isdisjoint(r["to"] for r in rows(self.state / "queue.csv")))

    def test_refuses_without_credentials_or_blocked_sender(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            code, _ = run("send", "--state", str(self.state), *SENDER, "--transport", "sendgrid", "--live", "--ignore-hours")
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

    def test_outlook_batch_and_record(self):
        code, out = run("send", "--state", str(self.state), *SENDER, "--transport", "outlook",
                        "--live", "--ignore-hours", "--daily-cap", "3")
        self.assertEqual(code, 0)
        batch = __import__("json").loads((self.state / "outbox.json").read_text())
        self.assertEqual(len(batch), 3)
        run("record", "--state", str(self.state), batch[0]["to"], batch[1]["to"])
        self.assertEqual(len(rows(self.state / "queue.csv")), 2)
        # Cap counts Outlook sends: only 1 more allowed today.
        run("send", "--state", str(self.state), *SENDER, "--transport", "outlook", "--live", "--ignore-hours", "--daily-cap", "3")
        self.assertEqual(len(__import__("json").loads((self.state / "outbox.json").read_text())), 1)

    def _age_log(self, days):
        import datetime as dtm
        log = rows(self.state / "sent_log.csv")
        for r in log:
            r["sent_at"] = (dtm.datetime.fromisoformat(r["sent_at"]) - dtm.timedelta(days=days)).isoformat()
        po.write_rows(self.state / "sent_log.csv", po.LOG_COLS, log)

    def _outlook_send(self, cap="40"):
        run("send", "--state", str(self.state), *SENDER, "--transport", "outlook", "--live", "--ignore-hours",
            "--daily-cap", cap, "--sender-address", "123 Main St")
        return __import__("json").loads((self.state / "outbox.json").read_text())

    def _fus(self, batch):
        return {b["to"]: b for b in batch if b["kind"] != "initial"}

    def test_followups_at_5_and_30_days(self):
        first = self._outlook_send(cap="2")
        self.assertTrue(all(b["kind"] == "initial" for b in first))
        run("record", "--state", str(self.state), *[b["to"] for b in first])
        a, b = first[0]["to"], first[1]["to"]

        self._age_log(4)                                   # day 4: nothing due
        self.assertFalse(self._fus(self._outlook_send(cap="2")))

        self._age_log(1)                                   # day 5: follow-up 1 for both
        batch = self._outlook_send(cap="5")
        fus = self._fus(batch)
        self.assertEqual(set(fus), {a, b})
        self.assertEqual({f["kind"] for f in fus.values()}, {"followup1"})
        self.assertEqual(batch[0]["kind"], "followup1")    # follow-ups go first
        self.assertTrue(fus[a]["subject"].startswith("RE: "))
        self.assertEqual(fus[a]["original_subject"], first[0]["subject"])
        self.assertIn("confidential call", fus[a]["body"])
        self.assertIn("123 Main St", fus[a]["body"])
        run("record", "--state", str(self.state), "--kind", "followup1", a, b)

        self._age_log(1)                                   # day 6: follow-up 1 not repeated
        self.assertFalse(self._fus(self._outlook_send(cap="5")))

        run("record", "--state", str(self.state), "--status", "replied", b)
        self._age_log(24)                                  # day 30: follow-up 2, only for a
        fus = self._fus(self._outlook_send(cap="5"))
        self.assertEqual(list(fus), [a])
        self.assertEqual(fus[a]["kind"], "followup2")
        self.assertIn("sounding board", fus[a]["body"])
        self.assertIn(first[0]["practice"].split(" and ")[0], fus[a]["body"])
        run("record", "--state", str(self.state), "--kind", "followup2", a)

        self._age_log(60)                                  # nothing after the second follow-up
        self.assertFalse(self._fus(self._outlook_send(cap="5")))

    def test_late_followup1_keeps_spacing(self):
        first = self._outlook_send(cap="1")
        run("record", "--state", str(self.state), first[0]["to"])
        self._age_log(29)                                   # follow-up 1 only goes out on day 29
        run("record", "--state", str(self.state), "--kind", "followup1", first[0]["to"])
        self._age_log(1)                                    # day 30, but only 1 day after follow-up 1
        self.assertFalse(self._fus(self._outlook_send()))
        self._age_log(24)                                   # 25 days after follow-up 1
        self.assertEqual([f["kind"] for f in self._fus(self._outlook_send()).values()], ["followup2"])

    def test_followup_skips_unsubscribed(self):
        first = self._outlook_send(cap="1")
        run("record", "--state", str(self.state), first[0]["to"])
        self._age_log(6)
        run("suppress", "--state", str(self.state), first[0]["to"])
        self.assertFalse(self._fus(self._outlook_send()))

    def test_domain_suppression_and_practice_wording(self):
        sup = po.Suppressed()
        for e in ["@dorsey.com/", "x@y.com"]:
            sup.add(e)
        self.assertIn("a.b@dorsey.com", sup)
        self.assertIn("a@mail.dorsey.com", sup)
        self.assertIn("X@Y.com", sup)
        self.assertNotIn("a@notdorsey.com", sup)
        self.assertEqual(po.display_practice(["GENERAL PRACTICE", "FRANCHISE", "LABOR & EMPLOYMENT"]),
                         "franchise and labor & employment")
        self.assertEqual(po.display_practice(["GENERAL PRACTICE"]), "general practice")
        self.assertEqual(po.parse_areas('{"REAL ESTATE",M&A}'), ["REAL ESTATE", "M&A"])

    def test_excluded_main_practice(self):
        import tempfile as tf
        f = Path(tf.mkdtemp()) / "c.csv"
        f.write_text("firstName,email,currentTitle,currentCompany,practiceAreas\n"
                     "A,a@x.com,Partner,X LLP,FAMILY LAW; DIVORCE\n"
                     "B,b@y.com,Partner,Y LLP,TAX; FAMILY LAW\n"
                     "C,c@z.com,Partner,Z LLP,PERSONAL INJURY\n")
        st = Path(tf.mkdtemp())
        run("prepare", "--state", str(st), *SENDER, "--contacts", str(f), "--sender-company", "V",
            "--sender-address", "X")
        self.assertEqual([r["to"] for r in rows(st / "queue.csv")], ["b@y.com"])

    def test_followups_never_open_with_a_reminder(self):
        from string import Template
        for n in (1, 2):
            tpl = Template((ROOT / "templates" / f"partner_followup_{n}_email.txt").read_text())
            body = po.render_body(tpl, {"first_name": "Ann", "practice_phrase": "tax practice",
                                        "practice_work": "tax work", "sender_signature": "J",
                                        "sender_address": "X", "unsubscribe_email": "j@v.com"})
            po.check_followup_opening(body)  # must not raise
        for bad in ["Just bumping my note from last week.", "Following up on my email.",
                    "I wanted to bring this to the top of your inbox.", "I wanted to bring this to the top of you're inbox.",
                    "Circling back on the below.", "Checking in on my last note.", "I reached out last month about roles.",
                    "In case this got buried, a quick question."]:
            with self.assertRaises(ValueError, msg=bad):
                po.check_followup_opening(f"Hi Ann,\n\n{bad} More text here.\n")

    def test_short_firm(self):
        cases = {
            "Dawda Mann Mulcahy & Sadler PLC": "Dawda Mann",
            "Honigman LLP Attorneys & Counselors": "Honigman",
            "Vorys, Sater, Seymour and Pease, LLP": "Vorys Sater",
            "Kaufman & Dolowich": "Kaufman",
            "Duane Morris LLP": "Duane Morris",
            "William Mattar, P.C.": "William Mattar",
            "Ruiz Gold PA": "Ruiz Gold",
            "Law Offices of Jane Doe, P.A.": "Jane Doe",
            "Taft Stettinius & Hollister LLP": "Taft Stettinius",
            "Couzens Lansky Fealk Ellis Roeder & Lazar, PC": "Couzens Lansky",
            "Smith & Associates, LLC": "Smith",
            "BECKER LAW PC": "Becker Law",
            "Clausen Miller, P.C.": "Clausen Miller",
            "Naman Howell Smith & Lee PLLC": "Naman Howell",
            "K & L Gates LLP": "K&L Gates",
            "K & L": "K&L",
            "J&G Law": "J&G Law",
        }
        for raw, want in cases.items():
            self.assertEqual(po.short_firm(raw), want, raw)

    def test_subject_rule(self):
        self.assertEqual(po.fix_subject("I’m house roles in NYC"), "In-house roles in NYC")


if __name__ == "__main__":
    unittest.main()
