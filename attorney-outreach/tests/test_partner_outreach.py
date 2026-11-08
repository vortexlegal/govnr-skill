"""Run: python3 -m unittest discover -s tests  (from attorney-outreach/)"""
import re, contextlib, csv, io, os, sys, tempfile, unittest
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
        self.assertFalse(po.is_partner("Partner Admin"))
        self.assertFalse(po.is_partner("Partner Emeritus"))

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
        self.assertIn("stronger platform", fus[a]["body"])
        self.assertIn("123 Main St", fus[a]["body"])
        self.assertTrue(fus[a]["body"].startswith("Hi "))
        # Sent directly (no draft): body_html carries the first email quoted underneath.
        self.assertIn("<hr>", fus[a]["body_html"])
        import html as _h
        self.assertIn("<b>Subject:</b> " + _h.escape(first[0]["subject"], quote=False), fus[a]["body_html"])
        self.assertIn(_h.escape(first[0]["body"].splitlines()[2][:40], quote=False), fus[a]["body_html"])
        run("record", "--state", str(self.state), "--kind", "followup1", a, b)

        self._age_log(1)                                   # day 6: follow-up 1 not repeated
        self.assertFalse(self._fus(self._outlook_send(cap="5")))

        run("record", "--state", str(self.state), "--status", "replied", b)
        self._age_log(24)                                  # day 30: follow-up 2, only for a
        fus = self._fus(self._outlook_send(cap="5"))
        self.assertEqual(list(fus), [a])
        self.assertEqual(fus[a]["kind"], "followup2")
        self.assertIn("No motion to compel.", fus[a]["body"])
        self.assertIn(first[0]["practice"], fus[a]["body"])
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
                     "Amy,a@x.com,Partner,X LLP,FAMILY LAW; DIVORCE\n"
                     "Bob,b@y.com,Partner,Y LLP,TAX; FAMILY LAW\n"
                     "Cal,c@z.com,Partner,Z LLP,PERSONAL INJURY\n")
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
            for seed in range(200):  # every wording variant
                po.check_followup_opening(po.render_body(tpl, {"first_name": "Ann", "practice_desc": "tax practice",
                                                               "sender_signature": "J"}, seed=str(seed)))
        for bad in ["Just bumping my note from last week.", "Following up on my email.",
                    "I wanted to bring this to the top of your inbox.", "I wanted to bring this to the top of you're inbox.",
                    "Circling back on the below.", "Checking in on my last note.", "I reached out last month about roles.",
                    "In case this got buried, a quick question.",
                    "Would you take a call? Just following up on the below.",
                    "One question. Bringing this to the top of your inbox.",
                    "Would you take a call? Wanted to bring this back up.",
                    "A quick nudge on my earlier note.", "Per my last email, would you take a call?",
                    "Resending in case you missed it."]:
            with self.assertRaises(ValueError, msg=bad):
                po.check_followup_opening(f"Hi Ann,\n\nA question first. {bad} More text here.\n")
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

    def test_plain_single_practice_and_variation(self):
        self.assertEqual(po.plain_practice(["LABOR & EMPLOYMENT", "ENVIRONMENT"]), "employment")
        self.assertEqual(po.plain_practice(["GENERAL PRACTICE", "TRUSTS & ESTATES"]), "estate planning")
        self.assertEqual(po.plain_practice(["REAL ESTATE", "CONSTRUCTION"]), "real estate")
        self.assertEqual(po.plain_practice([]), "")
        bodies = {r["body"].split("Best regards")[0] for r in rows(self.state / "queue.csv")}
        self.assertEqual(len(bodies), len(rows(self.state / "queue.csv")))  # no two identical
        for r in rows(self.state / "queue.csv"):
            b = r["body"]
            self.assertTrue(b.startswith("Hi "), b[:20])
            self.assertNotIn("Dear", b)
            self.assertNotRegex(b, r"\b(LLP|LLC|PLLC|P\.?A\.|P\.?C\.)\b")
            self.assertLessEqual(b.lower().count("confidential"), 1)
            self.assertIn("0.1", b)
            self.assertEqual(b.lower().count("stronger platform"), 1)
            self.assertNotIn("specific opening", b)
            self.assertIn("At your level, it's not like", b)
            self.assertLessEqual(b.lower().count("quick"), 1)
            self.assertIn("123 Main St", b)  # address in signature
            self.assertLess(len(b.split("Best regards")[0].split()), 110)
        # Same person always gets the same wording and subject.
        st2 = Path(tempfile.mkdtemp())
        run("prepare", "--state", str(st2), *SENDER, "--contacts", str(ROOT / "examples" / "crm_partners.csv"),
            "--sender-company", "Vortex Legal", "--sender-address", "123 Main St")
        self.assertEqual([(r["subject"], r["body"]) for r in rows(st2 / "queue.csv")],
                         [(r["subject"], r["body"]) for r in rows(self.state / "queue.csv")])

    def test_leadership_never_emailed(self):
        for t in ["Managing Partner", "Office Managing Partner", "Co-Managing Partner", "Deputy Managing Partner",
                  "Founding Partner", "Founder and Managing Partner", "Co-Founder", "CEO", "Chief Executive Officer",
                  "President", "Owner", "Managing Shareholder",
                  "Managing Member", "Executive Partner", "Name Partner"]:
            self.assertTrue(po.is_leadership(t), t)
        for t in ["Partner", "Equity Partner", "Shareholder", "Senior Partner", "Income Partner", "Principal",
                  "Chair, Litigation Group", "Partner and Co-Chair, Insurance Coverage", "Vice Chair", "Chairman"]:
            self.assertFalse(po.is_leadership(t), t)
        self.assertNotIn("tbecker@example.com", {r["to"] for r in rows(self.state / "queue.csv")})

    def test_greeting_name(self):
        self.assertEqual(po.greeting_name("E Chase"), "Chase")
        self.assertEqual(po.greeting_name("E."), "")
        self.assertEqual(po.greeting_name("K C"), "")
        self.assertEqual(po.greeting_name("Anne Marie"), "Anne Marie")
        self.assertEqual(po.greeting_name("Dana"), "Dana")
        self.assertEqual(po.greeting_name("Open"), "")
        self.assertEqual(po.name_from_email("lisa.colon@saul.com", "Colon"), "Lisa")
        self.assertEqual(po.name_from_email("drew.linnenbom@dinsmore.com", "Linnenbom"), "Drew")
        self.assertEqual(po.name_from_email("rlevy@offitkurman.com", "Levy"), "")
        self.assertEqual(po.name_from_email("info.dept@firm.com", "Levy"), "")

    def test_paragraphs_and_html(self):
        for r in rows(self.state / "queue.csv"):
            core = r["body"].split("Best regards")[0]
            paras = [p for p in core.split("\n\n") if p.strip()]
            self.assertGreaterEqual(len(paras), 5)                       # greeting + 4 short paragraphs
            self.assertTrue(all(len(re.split(r"(?<=[.?!])\s", p)) <= 2 for p in paras), paras)
        html = po.to_html("Hi Ann,\n\nLine one. Two.\n\nBest regards,\nJon\nVortexLegal & Co <x>\n")
        self.assertEqual(html, "<p>Hi Ann,</p><p>Line one. Two.</p><p>Best regards,<br>Jon<br>VortexLegal &amp; Co &lt;x&gt;</p>")

    def test_no_opt_out_sentence(self):
        for r in rows(self.state / "queue.csv"):
            self.assertNotRegex(r["body"].lower(), r"not reach out again|prefer i stop|unsubscribe")

    def test_separate_followup_cap(self):
        first = self._outlook_send(cap="3")
        run("record", "--state", str(self.state), *[b["to"] for b in first])
        self._age_log(5)
        # Follow-ups only (first-email cap 0), capped at 2.
        run("send", "--state", str(self.state), *SENDER, "--transport", "outlook", "--live", "--ignore-hours",
            "--daily-cap", "0", "--followup-daily-cap", "2", "--max-followups-per-run", "20", "--sender-address", "X")
        batch = __import__("json").loads((self.state / "outbox.json").read_text())
        self.assertEqual([b["kind"] for b in batch], ["followup1", "followup1"])
        run("send", "--state", str(self.state), *SENDER, "--transport", "outlook", "--live", "--ignore-hours",
            "--daily-cap", "5", "--followup-daily-cap", "0", "--per-firm-daily", "5", "--sender-address", "X")
        batch = __import__("json").loads((self.state / "outbox.json").read_text())
        self.assertTrue(batch and all(b["kind"] == "initial" for b in batch))

    def test_max_per_run(self):
        batch = __import__("json").loads("[]")
        run("send", "--state", str(self.state), *SENDER, "--transport", "outlook", "--live", "--ignore-hours",
            "--daily-cap", "40", "--max-per-run", "2", "--per-firm-daily", "5", "--sender-address", "X")
        batch = __import__("json").loads((self.state / "outbox.json").read_text())
        self.assertEqual(len(batch), 2)
        self.assertTrue(all(60 <= b["pause_seconds_before"] <= 300 for b in batch))
        self.assertTrue(all(b["body_html"].startswith("<p>Hi ") for b in batch))

    def test_summary(self):
        first = self._outlook_send(cap="2")
        run("record", "--state", str(self.state), first[0]["to"])
        run("record", "--state", str(self.state), "--status", "replied", "--detail", "interested", first[1]["to"])
        code, out = run("summary", "--state", str(self.state))
        self.assertEqual(code, 0)
        self.assertIn("First emails sent: 1", out)
        self.assertIn("Interested, follow up with:", out)
        self.assertIn(first[1]["to"], out)

    def test_subject_rule(self):
        self.assertEqual(po.fix_subject("I’m house roles in NYC"), "In-house roles in NYC")


class PersonalEmailTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.state = self.dir / "state"
        self.contacts = self.dir / "crm.csv"
        with open(self.contacts, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["firstName", "lastName", "email", "personal_email", "currentCompany", "currentTitle",
                        "practiceAreas"])
            w.writerow(["Dana", "Whit", "dana@harlow.com", "dana.w@gmail.com", "Harlow LLP", "Partner", "Tax"])
            w.writerow(["Marc", "Oye", "marc@gmail.com", "", "Bright LLP", "Partner", "Tax"])
            w.writerow(["Lee", "Park", "lee@park.com", "", "Park LLP", "Partner", "Tax"])
            w.writerow(["Ana", "Ruiz", "ana@ruiz.com", "ana.ruiz@icloud.com", "Ruiz PA", "Shareholder", "Tax"])
            w.writerow(["Kim", "Ng", "kim@ng.com", "kim.ng@yahoo.com", "Ng LLP", "Partner", "Tax"])
        self.verified = self.dir / "zb.csv"
        with open(self.verified, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Email Address", "ZB Status"])
            w.writerows([["dana.w@gmail.com", "valid"], ["marc@gmail.com", "invalid"],
                         ["ana.ruiz@icloud.com", "catch-all"], ["kim.ng@yahoo.com", "valid"]])

    def prepare(self, *extra):
        return run("prepare", "--state", str(self.state), *SENDER, "--contacts", str(self.contacts),
                   "--sender-address", "123 Main St", "--email-type", "personal", *extra)

    def test_personal_pick(self):
        self.assertEqual(po.personal_email({"email": "a@firm.com", "personal_email": "a@x.org"}), "a@x.org")
        self.assertEqual(po.personal_email({"email": "a@firm.com; a@gmail.com"}), "a@gmail.com")
        self.assertEqual(po.personal_email({"email": "a@firm.com"}), "")

    def test_export_then_verified_queue(self):
        out = self.dir / "to_verify.csv"
        code, _ = self.prepare("--export-emails", str(out))
        self.assertEqual(code, 0)
        self.assertEqual(sorted(r["email"] for r in rows(out)),
                         ["ana.ruiz@icloud.com", "dana.w@gmail.com", "kim.ng@yahoo.com", "marc@gmail.com"])
        self.assertEqual(rows(self.state / "queue.csv"), [])
        code, _ = self.prepare("--verified-file", str(self.verified), "--require-verified")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(r["to"] for r in rows(self.state / "queue.csv")),
                         ["dana.w@gmail.com", "kim.ng@yahoo.com"])
        reasons = {r["email"] or r["name"]: r["reason"] for r in rows(self.state / "prepare_skipped.csv")}
        self.assertIn("invalid", reasons["marc@gmail.com"])
        self.assertIn("catch-all", reasons["ana.ruiz@icloud.com"])
        self.assertEqual(reasons["Lee Park"], "no personal email on record")

    def test_verify_resumes_and_feeds_prepare(self):
        src = self.dir / "to_verify.csv"
        self.prepare("--export-emails", str(src))
        res = self.dir / "elv.csv"
        answers = {"dana.w@gmail.com": "ok", "marc@gmail.com": "email_disabled",
                   "ana.ruiz@icloud.com": "ok_for_all", "kim.ng@yahoo.com": "ok"}
        calls = []
        def fake(e, key, timeout=60):
            calls.append(e); return answers[e]
        with mock.patch.object(po, "elv_check", fake), mock.patch.object(po.time, "sleep"):
            self.assertEqual(run("verify", "--input", str(src), "--output", str(res), "--limit", "2")[0], 0)
            self.assertEqual(run("verify", "--input", str(src), "--output", str(res))[0], 0)
        self.assertEqual(len(calls), 4)  # second run only checked the remaining two
        self.prepare("--verified-file", str(res), "--require-verified")
        self.assertEqual(sorted(r["to"] for r in rows(self.state / "queue.csv")),
                         ["dana.w@gmail.com", "kim.ng@yahoo.com"])

    def test_associates(self):
        for title in ["Associate", "Senior Associate", "Associate Attorney", "Litigation Associate"]:
            self.assertTrue(po.is_associate(title), title)
        for title in ["Associate General Counsel", "Summer Associate", "Associate Partner", "Partner",
                      "Associate Director of Marketing", "Paralegal"]:
            self.assertFalse(po.is_associate(title), title)
        with open(self.contacts, "a", newline="") as f:
            csv.writer(f).writerow(["Rae", "Fox", "rae@fox.com", "rae.fox@gmail.com", "Fox LLP",
                                    "Senior Associate", "Tax"])
        state = self.dir / "assoc"
        code, out = run("prepare", "--state", str(state), *SENDER, "--contacts", str(self.contacts),
                        "--sender-address", "123 Main St", "--email-type", "personal", "--audience", "associate")
        self.assertEqual(code, 0)
        q = rows(state / "queue.csv")
        self.assertEqual([r["to"] for r in q], ["rae.fox@gmail.com"])
        self.assertNotIn("portable", q[0]["body"])
        self.assertIn("associate", q[0]["body"])

    def test_untitled_dnu_t_and_firm_addresses(self):
        p = self.dir / "crm2.csv"
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["first_name", "last_name", "personal_email", "work_email", "current_title",
                        "current_company", "practice_areas", "is_dnu"])
            w.writerow(["Al", "One", "al1@gmail.com", "", "", "One Law Group", "", "f"])
            w.writerow(["Fi", "Six", "fi6@gmail.com", "", "", "Walmart Global Tech", "", "f"])
            w.writerow(["Bo", "Two", "bo2@gmail.com", "", "", "Acme LLP", "Tax", "t"])
            w.writerow(["Cy", "Three", "cy3@jonesday.com", "", "", "Three Law PC", "", "f"])
            w.writerow(["Di", "Four", "di4@gmail.com", "di@jonesday.com", "Partner", "Jones Day", "Tax", "f"])
            w.writerow(["Ed", "Five", "ed5@edfive.com", "", "", "Acme LLP", "", "f"])
        state = self.dir / "untitled"
        code, _ = run("prepare", "--state", str(state), *SENDER, "--contacts", str(p),
                      "--sender-address", "123 Main St", "--email-type", "personal", "--audience", "untitled")
        self.assertEqual(code, 0)
        q = {r["to"]: r for r in rows(state / "queue.csv")}
        self.assertEqual(sorted(q), ["al1@gmail.com"])
        reasons = {r["email"]: r["reason"] for r in rows(state / "prepare_skipped.csv")}
        self.assertEqual(reasons["bo2@gmail.com"], "DNU / opted out in CRM")
        self.assertEqual(reasons["cy3@jonesday.com"], "personal-email field holds a firm address")
        self.assertIn("has a title", reasons["di4@gmail.com"])
        for r in q.values():
            self.assertNotIn("roles like yours", r["body"].lower())
            self.assertNotIn("your firm", r["body"].lower())
            self.assertNotIn("partner", r["body"].lower())
        self.assertIn("likely a firm mailbox", reasons["ed5@edfive.com"])
        self.assertEqual(reasons["fi6@gmail.com"], "no title, and employer isn't a law firm")
        self.assertTrue(po.looks_like_firm_lawyer("", "johnbhoffmanesq@gmail.com", "John Hoffman", set()))
        self.assertTrue(po.looks_like_firm_lawyer("Womble Bond", "y@gmail.com", "Y", {"womble bond"}))
        self.assertFalse(po.looks_like_firm_lawyer("Clayton County District Attorney's Office", "a@gmail.com", "A", set()))
        self.assertFalse(po.looks_like_firm_lawyer("", "dlc.beck@gmail.com", "Danny Beck", set()))
        self.assertFalse(po.looks_like_firm_lawyer("Legal Aid", "a@gmail.com", "A", set()))
        self.assertFalse(po.looks_like_firm_lawyer("Office of Principle Legal Advisor", "a@gmail.com", "A", set()))
        self.assertTrue(po.looks_like_firm_lawyer("Cozen O’Connor", "a@gmail.com", "A", {"cozen oconnor"} | {"cozen o"}))
        self.assertFalse(po.looks_like_firm_lawyer("Goldman Sachs & Co.", "a@gmail.com", "A", {"goldman ismail"}))
        self.assertTrue(po.looks_like_firm_lawyer("Law Offices of Amanda Chacon", "a@gmail.com", "A", set()))
        for f in ["Law Offices of Amanda Chacon", "The Law Office of Bree Rials, LLC", "LAW OFFICE", "The Law", "Law Offices"]:
            self.assertTrue(po.SOLO_PRACTICE_RE.search(f), f)
        for f in ["Jun He Law Offices LLC", "Lawyers for Justice, PC", "Sophia Law Group, LLP", "Lawson & Weitzen"]:
            self.assertFalse(po.SOLO_PRACTICE_RE.search(f), f)
        self.assertTrue(po.is_personal_domain("a@lawnet.ucla.edu"))
        self.assertTrue(po.is_personal_domain("a@alumni.stanford.edu"))
        self.assertFalse(po.is_personal_domain("a@shefskylaw.com"))
        self.assertTrue(po.is_personal_domain("a@edfive.com", {"edfive.com"}))
        self.assertIn("work at Acme", po.about_line("", "Acme", "x") + po.about_line("", "Acme", "y"))
        self.assertEqual(po.about_line("", "Law Offices", "x"), "")
        self.assertTrue(po.is_freemail("a@live.fr") and po.is_freemail("a@yahoo.co.in"))
        self.assertNotIn("\n\n\n", q["al1@gmail.com"]["body"])

    def test_take_over_queued(self):
        other = self.dir / "work"
        other.mkdir()
        po.write_rows(other / "queue.csv", po.QUEUE_COLS,
                      [{"to": "kim@ng.com", "name": "Kim Ng", "firm": "Ng LLP"},
                       {"to": "x@y.com", "name": "X Y", "firm": "Y LLP"}])
        self.prepare("--verified-file", str(self.verified), "--require-verified",
                     "--take-over-from", str(other))
        self.assertEqual(sorted(r["to"] for r in rows(self.state / "queue.csv")),
                         ["dana.w@gmail.com", "kim.ng@yahoo.com"])
        self.assertEqual([r["to"] for r in rows(other / "queue.csv")], ["x@y.com"])
        self.assertEqual([r["to"] for r in rows(other / "moved_to_other_campaign.csv")], ["kim@ng.com"])

    def test_no_like_yours_anywhere(self):
        for f in (ROOT / "templates").glob("*.txt"):
            self.assertNotIn("like yours", f.read_text().lower(), f.name)

    def test_per_firm_limit_off_and_placeholder_firms(self):
        q = [{"contact_key": str(i), "to": f"p{i}@gmail.com", "name": f"P{i} X", "firm": f, "title": "Partner",
              "practice": "", "subject": "Hi", "body": "Hi\n\nBest regards,\nJ", "prepared_at": ""}
             for i, f in enumerate(["Kirkland & Ellis LLP", "Kirkland & Ellis LLP", "Law Office", "Law Office"])]
        def batch(*extra):
            st = self.dir / ("s" + "".join(extra)); st.mkdir(exist_ok=True)
            po.write_rows(st / "queue.csv", po.QUEUE_COLS, q)
            run("send", "--state", str(st), *SENDER, "--sender-address", "1 Main", "--live", "--ignore-hours",
                "--max-per-run", "10", "--daily-cap", "10", *extra)
            return sorted(r["to"] for r in __import__("json").loads((st / "outbox.json").read_text()))
        self.assertEqual(batch(), ["p0@gmail.com", "p2@gmail.com", "p3@gmail.com"])  # placeholders aren't a firm
        self.assertEqual(len(batch("--per-firm-daily", "0")), 4)

    def test_placeholder_firms_and_subject_fit(self):
        for f in ["Independent Legal", "Law Offices", "Contract Attorney", "Legal Consultant", "Solo", "Attorney"]:
            self.assertTrue(po.GENERIC_FIRM_RE.match(f), f)
        for f in ["Kirkland", "Legal Aid Society", "Sophia Law Group, LLP"]:
            self.assertFalse(po.GENERIC_FIRM_RE.match(f), f)
        self.assertEqual(po.about_line("", "Independent Legal", "x"), "")
        p = self.dir / "crm3.csv"
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["first_name", "last_name", "personal_email", "current_title", "current_company", "practice_areas"])
            for i in range(30):
                w.writerow([f"Pat{i}", "Lee", f"pat{i}@gmail.com", "Partner", "Independent Legal", ""])
        state = self.dir / "fit"
        run("prepare", "--state", str(state), *SENDER, "--contacts", str(p), "--sender-address", "1 Main",
            "--email-type", "personal")
        for r in rows(state / "queue.csv"):
            self.assertNotIn("Independent Legal", r["body"] + r["subject"])
            self.assertNotIn("your firm", r["subject"].lower())
            self.assertNotIn("$", r["subject"])
            self.assertNotRegex(r["subject"], r"\bpartners are landing|your\s+book")

    def test_firm_domains_ignore_copied_personal_and_school(self):
        doms = po.firm_domains([], [{"personal_email": "ash@lsu.edu", "work_email": "ash@lsu.edu"},
                                    {"personal_email": "bo@gmail.com", "work_email": "bo@bigfirm.com"},
                                    {"personal_email": "cy@cyfamily.net", "work_email": "cy@cyfamily.net"},
                                    {"personal_email": "di@gmail.com", "work_email": "di@columbia.edu"}])
        self.assertEqual(doms, {"bigfirm.com"})

    def test_require_verified_needs_file(self):
        code, _ = self.prepare("--require-verified")
        self.assertEqual(code, 2)

    def test_other_campaign_skipped(self):
        other = self.dir / "work-state"
        other.mkdir()
        with open(other / "sent_log.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(po.LOG_COLS)
            w.writerow(["2026-10-01T10:00:00+00:00", "kim@ng.com", "Kim Ng", "Ng LLP", "s", "outlook", "sent",
                        "", "initial", "tax"])
        self.prepare("--verified-file", str(self.verified), "--require-verified", "--other-state", str(other))
        self.assertEqual([r["to"] for r in rows(self.state / "queue.csv")], ["dana.w@gmail.com"])


if __name__ == "__main__":
    unittest.main()
