import argparse
import json
import os
import re
import smtplib
import sqlite3
import ssl
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from lxml import etree, html
from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent
MCP = FastMCP("Recent job applications")
ADDRESS = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
UTC = timezone.utc


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch(url):
    if urlsplit(url).scheme != "https":
        raise ValueError("Source requires HTTPS")
    req = Request(url, headers={"User-Agent": "JobApplicationReader/1.0"})
    with build_opener(NoRedirect()).open(req, timeout=12) as response:
        data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError("Source response exceeds 2 MB")
        return data


def settings():
    cfg = json.loads((ROOT / "settings.json").read_text(encoding="utf8"))
    for key in ("titles", "locations", "exclude_titles"):
        if not isinstance(cfg[key], list) or any(not isinstance(v, str) or not v.strip() for v in cfg[key]):
            raise ValueError(f"Invalid filter {key}")
    if not cfg["titles"] or not cfg["locations"] or cfg["max_age_hours"] <= 0 or cfg["daily_limit"] < 1:
        raise ValueError("Set title, location, age and daily limit")
    return cfg


def matches(value, terms):
    return any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", value, re.I) for term in terms)


def page_info(body):
    page = html.fromstring(body)
    def elements(cls):
        return page.xpath("//*[contains(concat(' ',normalize-space(@class),' '), $token)]", token=f" {cls} ")
    loc = " ".join(el.text_content().strip() for el in elements("location"))
    title = " ".join(page.xpath("//h1[1]//text()")).strip()
    blocks = elements("application_details")
    emails = set()
    for el in blocks:
        content = " ".join(el.text_content().split())
        match = re.search(r"to apply for this job email your details to\s+(" + ADDRESS + ")", content, re.I)
        if match:
            emails.add(match[1].rstrip("."))
    return title, loc, emails.pop() if len(emails) == 1 else ""


def scan(cfg):
    now = datetime.now(UTC)
    jobs, errors = {}, []
    for src in cfg["sources"]:
        try:
            xml = etree.fromstring(fetch(src["feed"]), etree.XMLParser(resolve_entities=False, no_network=True))
            if xml.tag != "rss":
                raise ValueError("Expected RSS job feed")
            for item in xml.findall("./channel/item"):
                url = (item.findtext("link") or "").strip()
                if not url.startswith(src["listing_prefix"]) or urlsplit(url).netloc != urlsplit(src["listing_prefix"]).netloc:
                    continue
                parts = urlsplit(url)
                url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/", "", ""))
                try:
                    date = parsedate_to_datetime(item.findtext("pubDate") or "")
                    if date.tzinfo is None or not timedelta() <= now-date <= timedelta(hours=cfg["max_age_hours"]):
                        continue
                    title = item.findtext("title") or ""
                    if not matches(title, cfg["titles"]) or matches(title, cfg["exclude_titles"]):
                        continue
                    title, location, to = page_info(fetch(url))
                    if not matches(title, cfg["titles"]) or matches(title, cfg["exclude_titles"]) or not matches(location, cfg["locations"]):
                        continue
                    domain = to.partition("@")[2].lower()
                    allowed = domain in src["recipient_domains"] and bool(to)
                    jobs[url] = dict(title=title, location=location, posted=date.astimezone(UTC).isoformat(),
                                     url=url, recipient=to if allowed else "", route="email" if allowed else "manual")
                except Exception as exc:
                    errors.append(f"{url} | {type(exc).__name__}: {exc}")
        except Exception as exc:
            errors.append(f"{src['feed']} | {type(exc).__name__}: {exc}")
    return {"jobs": sorted(jobs.values(), key=lambda j: j["posted"], reverse=True), "source_errors": errors}


def database():
    db = sqlite3.connect(ROOT / "applications.sqlite3", timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS applications(url TEXT PRIMARY KEY, title TEXT, recipient TEXT, started TEXT, status TEXT)")
    return db


def reserve(db, job, limit):
    now = datetime.now(UTC).isoformat()
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM applications WHERE url=?", (job["url"],)).fetchone():
            return "already recorded"
        if db.execute("SELECT count(*) FROM applications WHERE started >= ?", (now[:10],)).fetchone()[0] >= limit:
            return "daily limit"
        db.execute("INSERT INTO applications VALUES(?,?,?,?,?)",
                   (job["url"], job["title"], job["recipient"], now, "check before retry"))
    return "reserved"


def deliver(cfg, jobs, db):
    if not jobs:
        return []
    password = os.environ.get("JOB_SMTP_PASSWORD")
    if not password or not cfg["smtp_host"] or not re.fullmatch(ADDRESS, cfg["sender"]):
        raise ValueError("Configure sender, SMTP host and JOB_SMTP_PASSWORD")
    cv = (ROOT / cfg["cv"]).read_bytes()
    if not cv.startswith(b"%PDF-"):
        raise ValueError("Configured CV must be a PDF")
    results = []
    with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=30, context=ssl.create_default_context()) as mail:
        mail.login(os.environ.get("JOB_SMTP_USER") or cfg["sender"], password)
        for job in jobs:
            msg = EmailMessage()
            msg["From"], msg["To"] = cfg["sender"], job["recipient"]
            msg["Subject"] = f"Application for {job['title']} | {cfg['name']}"
            msg.set_content(f"Dear Hiring Team,\n\nPlease consider my application for the {job['title']} vacancy. My CV is attached.\n\nKind regards,\n{cfg['name']}")
            msg.add_attachment(cv, maintype="application", subtype="pdf", filename=Path(cfg["cv"]).name)
            status = reserve(db, job, cfg["daily_limit"])
            if status == "reserved":
                try:
                    mail.send_message(msg)
                    status = "accepted by mail server"
                except (OSError, smtplib.SMTPException):
                    status = "check before retry"
                with db:
                    db.execute("UPDATE applications SET status=? WHERE url=?", (status, job["url"]))
            results.append({"url": job["url"], "status": status})
            if status in ("check before retry", "daily limit"):
                break
    return results


@MCP.tool()
def find_recent_jobs() -> dict:
    """Scrape configured feeds and return recent matching jobs and source errors."""
    return scan(settings())


@MCP.tool()
def apply_to_recent_jobs() -> dict:
    """Send configured CV to matching email vacancies when enabled in local settings."""
    cfg = settings()
    result = scan(cfg)
    result["enabled"] = cfg["enabled"] is True
    result["applications"] = []
    if result["enabled"]:
        with closing(database()) as db:
            eligible = [j for j in result["jobs"] if j["route"] == "email"]
            result["applications"] = deliver(cfg, eligible, db)
    return result


@MCP.tool()
def application_history() -> list[dict]:
    """Return the 100 most recent delivery records."""
    with closing(database()) as db:
        return [dict(r) for r in db.execute("SELECT * FROM applications ORDER BY started DESC LIMIT 100")]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["server", "preview", "apply", "history"], default="server", nargs="?")
    command = parser.parse_args().command
    if command == "server":
        MCP.run(transport="stdio")
    else:
        import sys
        output = {"preview": find_recent_jobs, "apply": apply_to_recent_jobs, "history": application_history}[command]()
        print(json.dumps(output, indent=2))
        if isinstance(output, dict) and output.get("source_errors"):
            sys.exit(2)
