import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import jobs


class Workflow(unittest.TestCase):
    def test_workflow(self):
        cfg = jobs.settings()
        src = cfg["sources"][0]
        cfg.update(sources=[src], daily_limit=1, sender="me@example.org", smtp_host="smtp.example.org")
        now = datetime.now(jobs.UTC)
        def row(slug, age):
            return f"<item><title>Data Engineer</title><link>{src['listing_prefix']}{slug}/</link><pubDate>{format_datetime(now-timedelta(hours=age))}</pubDate></item>"
        feed = ("<rss><channel>" + row("old", 80) + row("new", 1) + row("portal", 2) + "</channel></rss>").encode()
        page = b'<h1>Data Engineer</h1><li class="location">Johannesburg</li>'
        email = b'<div class="application_details">To apply for this job email your details to careers@samford.co.za</div>'
        def fetch(url):
            if url == src["feed"]:
                return feed
            if url.endswith("/old/"):
                self.fail("Fetched stale vacancy")
            return page + (email if url.endswith("/new/") else b'')
        with tempfile.TemporaryDirectory() as tmp, patch.object(jobs, "ROOT", Path(tmp)), patch.object(jobs, "settings", return_value=cfg), patch.object(jobs, "fetch", side_effect=fetch), patch.dict("os.environ", JOB_SMTP_PASSWORD="test"), patch.object(jobs.smtplib, "SMTP_SSL") as smtp:
            (Path(tmp) / "cv.pdf").write_bytes(b"%PDF-test")
            preview = jobs.apply_to_recent_jobs()
            self.assertEqual(preview["source_errors"], [])
            self.assertEqual([j["route"] for j in preview["jobs"]], ["email", "manual"])
            smtp.assert_not_called()
            cfg["enabled"] = True
            result = jobs.apply_to_recent_jobs()
            self.assertEqual(result["applications"][0]["status"], "accepted by mail server")
            self.assertEqual(jobs.apply_to_recent_jobs()["applications"][0]["status"], "already recorded")
            msg = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertEqual(list(msg.iter_attachments())[0].get_payload(decode=True), b"%PDF-test")
            self.assertEqual(smtp.return_value.__enter__.return_value.send_message.call_count, 1)
            with closing(jobs.database()) as db:
                self.assertEqual(jobs.reserve(db, dict(result["jobs"][0], url="another"), 1), "daily limit")
            self.assertEqual(len(jobs.application_history()), 1)
