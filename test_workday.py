import os
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

import workday as wd

BOARD = "https://sample.wd3.myworkdayjobs.com/Careers"
URL = BOARD + "/job/Gauteng/Support_R1"


class Discovery(unittest.TestCase):
    def test_board_and_role_filters(self):
        self.assertEqual(wd.board_of(URL.replace("/Careers", "/en-US/Careers")), BOARD)
        self.assertFalse(wd.same_job(URL + "2", URL))
        with self.assertRaises(ValueError):
            wd.board_of(URL.replace(".com/", ".com.example/"))
        cfg = dict(titles=["developer"], workday_titles=[], locations=["Gauteng"],
                   exclude_titles=[], max_age_hours=72)
        listing = {"title": "Support", "externalPath": "/job/Gauteng/Support_R1"}
        detail = {"jobPostingInfo": {"jobReqId": "R1", "location": "Gauteng",
                                      "startDate": date.today().isoformat()}}
        with patch.object(wd, "get_json", side_effect=[{"total": 1, "jobPostings": [listing]}, detail]):
            self.assertEqual(list(wd.listings(BOARD, cfg)), [(BOARD+"|R1", "Support", URL, "R1", "")])

    def test_mail_retry(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(wd, "ROOT", Path(directory)), closing(wd.database()) as db:
            db.execute("INSERT INTO drafts(id,title,url,ref,state,pending) VALUES (?,?,?,?,?,1)",
                       ("1","Support",URL,"R1","Ready to submit"))
            db.commit()
            with patch.object(wd,"notify",side_effect=OSError), self.assertRaises(OSError):
                wd.flush(db,{})
            with patch.object(wd,"notify") as mail:
                wd.flush(db,{})
                wd.flush(db,{})
                mail.assert_called_once()


class Wizard(unittest.IsolatedAsyncioTestCase):
    async def test_stops_at_review(self):
        async with wd.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH"))
            try:
                page = await browser.new_page()
                html = """<h1>Application Questions</h1><label>City<input></label>
                    <button onclick="if(document.querySelector('input').value==='Johannesburg'){
                      document.querySelector('h1').textContent='Review';this.textContent='Submit';
                      this.onclick=()=>window.submitted=true}">Save and Continue</button>"""
                await page.route(URL, lambda route: route.fulfill(body=html, content_type="text/html"))
                result = await wd.prepare(page, URL, {"workday_answers": {"City":"Johannesburg"}})
                self.assertEqual(result[0], "Ready to submit")
                self.assertFalse(await page.evaluate("Boolean(window.submitted)"))
            finally:
                await browser.close()

    async def test_scheduled_discovery_upload_and_review_email(self):
        import hashlib
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock
        import pymupdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with pymupdf.open() as master:
                master.new_page().insert_text((50, 80), "Developed SQL pipelines for data quality checks and business reporting.")
                master.save(root / "master.pdf")
            cfg = dict(workday_enabled=True, workday_boards=[BOARD], titles=["Support"],
                       locations=["Gauteng"], exclude_titles=[], max_age_hours=72,
                       sender="candidate@example.org", smtp_host="smtp.example.org", smtp_port=465,
                       name="Test Candidate", cv="master.pdf")
            detail = {"jobPostingInfo": {"jobReqId":"R1", "startDate":date.today().isoformat(),
                      "location":"Gauteng", "jobDescription":"<p>SQL reporting and data quality</p>"}}
            feed = {"total":1,"jobPostings":[{"title":"Support", "externalPath":"/job/Gauteng/Support_R1"}]}
            wizard = """<h1>Autofill with Resume</h1><span></span>
                <input type=file onchange="document.querySelector('span').textContent=this.files[0].name">
                <button onclick="document.querySelector('h1').textContent='Review';this.textContent='Submit';
                this.onclick=()=>window.submitted=true">Save and Continue</button>"""
            async with wd.async_playwright() as p:
                browser = await p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH"))
                ctx = await browser.new_context()
                uploaded = []
                page = await ctx.new_page()
                await page.expose_function("recordUpload", lambda values: uploaded.append(bytes(values)))
                wizard = wizard.replace("this.files[0].name", "this.files[0].name;this.files[0].arrayBuffer().then(b=>recordUpload(Array.from(new Uint8Array(b))))")
                await ctx.route(URL, lambda route: route.fulfill(body=wizard, content_type="text/html"))
                launcher = SimpleNamespace(launch_persistent_context=AsyncMock(return_value=ctx))
                engine = MagicMock()
                engine.__aenter__ = AsyncMock(return_value=SimpleNamespace(chromium=launcher))
                with patch.object(wd,"ROOT",root), patch.object(wd,"async_playwright",return_value=engine), \
                     patch.dict(os.environ, JOB_SMTP_PASSWORD="demo", BRAVE_SEARCH_API_KEY=""), \
                     patch.object(wd,"get_json",side_effect=[feed,detail]), patch.object(wd.smtplib,"SMTP_SSL") as smtp:
                    self.assertEqual(await wd.worker(cfg), {"checked":1,"source_errors":[]})
                    msg = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
                    self.assertEqual(str(msg["To"]), cfg["sender"])
                    self.assertIn("Ready to submit", str(msg["Subject"]))
                    content = next(msg.iter_attachments()).get_payload(decode=True)
                    self.assertEqual(uploaded, [content])
                    with closing(wd.database()) as db:
                        state, digest, pending = db.execute("SELECT state,cv_hash,pending FROM drafts").fetchone()
                    self.assertEqual((state,digest,pending), ("Ready to submit",hashlib.sha256(content).hexdigest(),0))
                    with pymupdf.open(stream=content, filetype="pdf") as cv:
                        self.assertEqual(len(cv),2)
                        self.assertIn("SQL pipelines",cv[0].get_text())
                        self.assertIn("SQL pipelines",cv[1].get_text())
                await browser.close()
