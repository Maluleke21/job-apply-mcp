"""Discover Workday vacancies, prepare drafts and notify the candidate."""
import argparse
import asyncio
import hashlib
import json
import os
import re
import smtplib
import sqlite3
import ssl
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from lxml import html
from vacancy_cv import CVError, prepare_cv
from jobs import ADDRESS, NoRedirect, ROOT, matches
from workday_pilot import correct_experience, entry, phase, visible_one
from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright

MCP = FastMCP("Workday draft applications")
DEFAULT_BOARDS = ["https://firstrand.wd3.myworkdayjobs.com/FRB",
                  "https://myhcm.wd3.myworkdayjobs.com/Betway_Africa",
                  "https://absa.wd3.myworkdayjobs.com/ABSAcareersite"]
CAREER_TITLES = ["developer", "software engineer", "data engineer", "data analyst",
                 "data scientist", "data modeller", "analytics engineer", "BI analyst",
                 "business intelligence", "ETL engineer", "AI engineer", "AI trainer",
                 "AI data trainer", "machine learning engineer", "test analyst", "QA tester"]
BROWSER_DIR = Path.home() / ".job-apply-workday-browser"


def board_of(url):
    u = urlsplit(url)
    if u.scheme != "https" or not re.fullmatch(r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com", u.netloc):
        raise ValueError("Unsupported Workday board host")
    parts = u.path.strip("/").split("/")
    if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts = parts[1:]
    if not parts or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[0]):
        raise ValueError("Missing Workday site name")
    return f"https://{u.netloc}/{parts[0]}"


def get_json(url, payload=None, headers=None):
    req = Request(url, data=None if payload is None else json.dumps(payload).encode(),
                  headers={"Accept": "application/json", "Content-Type": "application/json",
                           "User-Agent": "JobApplicationReader/2.0", **(headers or {})})
    with build_opener(NoRedirect()).open(req, timeout=20) as res:
        return json.loads(res.read(4_000_000))


def discover_boards(cfg):
    boards = {board_of(b) for b in cfg.get("workday_boards", DEFAULT_BOARDS)}
    key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if key:
        for location in cfg["locations"]:
            query = urlencode({"q": f'site:myworkdayjobs.com inurl:job "{location}"', "count": 20})
            results = get_json("https://api.search.brave.com/res/v1/web/search?" + query,
                               headers={"X-Subscription-Token": key})
            for item in results["web"]["results"]:
                try:
                    boards.add(board_of(item["url"]))
                except ValueError:
                    continue
    return sorted(boards)


def listings(board, cfg):
    host, site = urlsplit(board).netloc, urlsplit(board).path.strip("/")
    api = f"https://{host}/wday/cxs/{host.split('.')[0]}/{site}"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg["max_age_hours"])
    requests = 0

    def fetch(label, url, payload=None):
        nonlocal requests
        if requests:
            time.sleep(1)
        requests += 1
        try:
            return get_json(url, payload)
        except HTTPError as exc:
            exc.add_note(f"Board request {requests}: {label}")
            raise

    offset = 0
    while True:
        data = fetch(f"listings offset {offset}", api + "/jobs", {"limit": 20, "offset": offset, "searchText": "", "appliedFacets": {}})
        rows = data["jobPostings"]
        for row in rows:
            title, path = row["title"], row["externalPath"]
            age = re.fullmatch(r"Posted (\d+)(\+?) Days? Ago", row.get("postedOn", ""), re.I)
            if age and int(age[1]) > cfg["max_age_hours"] / 24 + 1:
                continue
            if not path.startswith("/job/") or ".." in path or "?" in path:
                continue
            titles = cfg.get("workday_titles", [*cfg["titles"], *CAREER_TITLES])
            if (titles and not matches(title, titles)) or matches(title, cfg["exclude_titles"]):
                continue
            info = fetch(f"vacancy details at listing offset {offset}", api + path)["jobPostingInfo"]
            place = " ".join(filter(None, [info.get("location"), row.get("locationsText"),
                                            *(info.get("additionalLocations") or [])]))
            if not matches(place, cfg["locations"]):
                continue
            posted = datetime.fromisoformat(info["startDate"]).replace(tzinfo=timezone.utc)
            if posted.date() < cutoff.date():
                continue
            ref = info.get("jobReqId")
            if ref:
                description = info.get("jobDescription") or ""
                plain = " ".join(html.fromstring(description).itertext()) if description.strip() else ""
                yield (board + "|" + ref, title, board + path, ref, plain)
        offset += len(rows)
        if not rows or offset >= data["total"]:
            return


def same_job(current, url):
    try:
        if board_of(current) != board_of(url):
            return False
        slug = urlsplit(url).path.rstrip("/").split("/")[-1]
        return slug in urlsplit(current).path.split("/")
    except ValueError:
        return False


async def fill_answers(page, answers):
    for label, value in answers.items():
        field = await visible_one(page.get_by_label(label, exact=True))
        if field is None or not await field.is_enabled():
            continue
        tag, kind = await field.evaluate("e => [e.tagName,e.type]")
        if tag == "SELECT":
            await field.select_option(label=str(value))
        elif kind == "checkbox" and isinstance(value, bool):
            await field.set_checked(value)
        elif tag == "TEXTAREA" or kind in ("text", "email", "tel", "number"):
            await field.fill(str(value))
        else:
            radio = await visible_one(field.get_by_role("radio", name=str(value), exact=True))
            if radio:
                await radio.check()


async def prepare(page, url, cfg):
    await page.goto(url, wait_until="domcontentloaded")
    observed_cv = False
    for _ in range(20):
        # Wait for Workday's client-rendered wizard or posting controls.
        await page.locator('h1:visible,h2:visible,button:visible,[aria-current="step"]:visible').first.wait_for()
        if not same_job(page.url, url):
            return "Needs attention", "Sign in or reopen the matching requisition."
        if await page.get_by_text(re.compile(r"^(already applied|you have already applied.*)$", re.I)).first.is_visible():
            return "Already applied", ""
        step = await phase(page)
        if step == "Review":
            if cfg.get("verify_cv") and not observed_cv:
                filename = Path(cfg["cv"]).name
                if not await page.get_by_text(filename, exact=True).first.is_visible():
                    return "Needs attention", "Review reached. Verify the attached CV in this existing draft."
            return "Ready to submit", "Open the posting, resume your draft, review it and click Submit."
        if await page.locator('input[type=password]:visible').count():
            return "Needs attention", "Sign in to the employer account in the worker browser."
        if not step:
            for label in ("Continue Application", "Autofill with Resume", "Apply"):
                if await entry(page, label):
                    break
            else:
                return "Needs attention", "Open the posting to check sign-in or the current application step."
        else:
            if step == "Autofill with Resume":
                cv = ROOT / cfg["cv"]
                field = page.locator('input[type=file]')
                if not await page.get_by_text(cv.name, exact=True).first.is_visible():
                    if await field.count() != 1 or cv.stat().st_size > 5_000_000 or cv.read_bytes()[:5] != b"%PDF-":
                        return "Needs attention", "Check the resume or upload control."
                    await field.set_input_files(cv)
                    await page.get_by_text(cv.name, exact=True).first.wait_for(timeout=15000)
                observed_cv = True
            if step == "My Experience":
                await correct_experience(page)
            await fill_answers(page, cfg.get("workday_answers", {}))
            if not await entry(page, "Save and Continue") and not await entry(page, "Continue"):
                return "Needs attention", f"Open the {step} step to continue."
        for _ in range(24):
            await asyncio.sleep(.25)
            if not same_job(page.url, url) or await phase(page) != step:
                break
            errors = await page.locator('[role=alert]:visible,[data-automation-id="errorMessage"]:visible').all_inner_texts()
            if any(e.strip() for e in errors):
                return "Needs attention", f"{step or 'Application'}: " + " ".join(errors)[:350]
        else:
            if step:
                return "Needs attention", f"Check required fields on {step}."
    return "Needs attention", "The employer workflow needs manual review."


def notify(cfg, subject, body, attachment=None):
    recipient = cfg.get("workday_notify_to") or cfg["sender"]
    if not re.fullmatch(ADDRESS, recipient):
        raise ValueError("Set a valid candidate notification address")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["sender"], recipient, subject
    msg.set_content(body)
    if attachment:
        filename, content = attachment
        msg.add_attachment(content, maintype="application", subtype="pdf", filename=filename)
    with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=30, context=ssl.create_default_context()) as mail:
        mail.login(os.environ.get("JOB_SMTP_USER") or cfg["sender"], os.environ["JOB_SMTP_PASSWORD"])
        mail.send_message(msg)


def database():
    db = sqlite3.connect(ROOT / "workday.sqlite3", timeout=30)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS boards(url TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS cooldowns(board TEXT PRIMARY KEY, until INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY,title TEXT,url TEXT,ref TEXT,
          state TEXT DEFAULT 'Queued',detail TEXT DEFAULT '',pending INTEGER DEFAULT 0,checked TEXT DEFAULT '');
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(drafts)")}
    with db:
        for name in ("description", "cv_path", "cv_hash"):
            if name not in columns:
                db.execute(f"ALTER TABLE drafts ADD COLUMN {name} TEXT DEFAULT ''")
    return db


def flush(db, cfg, only_board=None):
    for key,title,url,ref,state,detail,path,digest in db.execute(
            "SELECT id,title,url,ref,state,detail,cv_path,cv_hash FROM drafts WHERE pending=1 "
            "AND (? IS NULL OR instr(id, ? || '|') = 1)", (only_board, only_board)).fetchall():
        employer = urlsplit(url).hostname.split(".")[0]
        attachment = None
        if path and state == "Ready to submit":
            pdf = ROOT / path
            content = pdf.read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                raise CVError("Stored review attachment has changed.")
            attachment = pdf.name, content
        notify(cfg, f"{state} — {employer} — {title} ({ref})", f"{detail}\n\n{title}\n{ref}\n{url}", attachment)
        with db:
            db.execute("UPDATE drafts SET pending=0 WHERE id=?", (key,))


def retry_time(header, now):
    fallback = now + timedelta(hours=4)
    try:
        if header and header.isascii() and header.isdigit():
            retry = now + timedelta(seconds=int(header))
        else:
            retry = parsedate_to_datetime(header)
        return max(now + timedelta(minutes=1), retry)
    except (TypeError, ValueError, OverflowError, AttributeError):
        return fallback


def scan_boards(db, cfg, errors, only_board=None):
    deferred = {}
    for board, until in db.execute(
        "SELECT url, until FROM boards LEFT JOIN cooldowns ON boards.url=cooldowns.board "
        "WHERE ? IS NULL OR url=?", (only_board, only_board)
    ).fetchall():
        now = datetime.now(timezone.utc)
        if until and until > now.timestamp():
            deferred[board] = datetime.fromtimestamp(until, timezone.utc).isoformat()
            continue
        try:
            for job in listings(board, cfg):
                with db:
                    db.execute("INSERT INTO drafts(id,title,url,ref,description) VALUES (?,?,?,?,?) "
                               "ON CONFLICT(id) DO UPDATE SET description=excluded.description", job)
        except HTTPError as exc:
            if exc.code == 429:
                retry = retry_time((exc.headers or {}).get("Retry-After"), datetime.now(timezone.utc))
                with db:
                    db.execute("INSERT OR REPLACE INTO cooldowns VALUES (?,?)", (board, int(retry.timestamp())))
                deferred[board] = retry.isoformat()
            context = " ".join(getattr(exc, "__notes__", ()))
            errors.append(f"{board}: HTTP {exc.code} {exc.reason} {context}".rstrip())
        except Exception as exc:
            errors.append(f"{board}: {type(exc).__name__}")
    return deferred


async def worker(cfg):
    if cfg.get("workday_enabled") is not True:
        return {"status": "Workday disabled"}
    errors = []
    async with async_playwright() as browser:
        ctx = await browser.chromium.launch_persistent_context(str(BROWSER_DIR), headless=True)
        try:
            with closing(database()) as db:
                only_board = board_of(cfg["workday_only_board"]) if cfg.get("workday_only_board") else None
                flush(db, cfg, only_board)
                boards = {only_board} if only_board else {board_of(b) for b in cfg.get("workday_boards", DEFAULT_BOARDS)}
                if not only_board:
                    try:
                        boards.update(discover_boards(cfg))
                    except Exception as exc:
                        errors.append(f"Employer search: {type(exc).__name__}")
                with db:
                    db.executemany("INSERT OR IGNORE INTO boards VALUES (?)", [(b,) for b in boards])
                deferred = scan_boards(db, cfg, errors, only_board)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                page.set_default_timeout(10000)
                todo = db.execute("""SELECT id,url,state,detail,title,description,cv_path,cv_hash FROM drafts
                    WHERE state IN ('Queued','Needs attention') AND (? IS NULL OR instr(id, ? || '|') = 1)
                    ORDER BY checked,id LIMIT ?""",
                                  (only_board, only_board, cfg.get("workday_batch_size", 10))).fetchall()
                for key,url,old,old_detail,title,description,path,digest in todo:
                    try:
                        if not path:
                            pdf, digest = prepare_cv(ROOT, cfg, key, title, description)
                            path = str(pdf.relative_to(ROOT))
                            with db:
                                db.execute("UPDATE drafts SET cv_path=?,cv_hash=? WHERE id=?", (path, digest, key))
                        pdf = ROOT / path
                        if hashlib.sha256(pdf.read_bytes()).hexdigest() != digest:
                            raise CVError("The archived CV changed. Restore the original PDF before continuing.")
                        state, detail = await prepare(page, url, dict(cfg, cv=str(pdf), verify_cv=True))
                    except CVError as exc:
                        state, detail = "Needs attention", str(exc)
                    except Exception as exc:
                        state, detail = "Needs attention", f"Could not finish this draft ({type(exc).__name__}). Open the posting."
                    with db:
                        db.execute("UPDATE drafts SET state=?,detail=?,pending=?,checked=? WHERE id=?",
                                   (state, detail, int(state != "Already applied" and (old,old_detail) != (state,detail)),
                                    datetime.now(timezone.utc).isoformat(), key))
                    flush(db, cfg, only_board)
                if errors:
                    notify(cfg, "Workday discovery needs attention", "\n".join(errors))
                return {"checked": len(todo), "source_errors": errors, **({"deferred_boards": deferred} if deferred else {})}
        finally:
            await ctx.close()


def config():
    return json.loads((ROOT / "settings.json").read_text(encoding="utf-8-sig"))


@MCP.tool()
async def prepare_workday_drafts() -> dict:
    """Scan employer boards, prepare matching drafts and email the candidate."""
    return await worker(config())


@MCP.tool()
def workday_history() -> list[dict]:
    """List draft states, links and last check times."""
    with closing(database()) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT title,ref,url,state,detail,checked FROM drafts ORDER BY checked DESC")]


async def login(url):
    board_of(url)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(str(BROWSER_DIR), headless=False)
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url)
            await asyncio.to_thread(input, "Sign in locally, then press Enter to save the session: ")
        finally:
            await ctx.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["scan", "history", "login", "server"])
    parser.add_argument("url", nargs="?")
    args = parser.parse_args()
    if args.mode == "server":
        MCP.run(transport="stdio")
    elif args.mode == "history":
        print(json.dumps(workday_history(), indent=2))
    elif args.mode == "login":
        if not args.url:
            parser.error("login requires an employer board or vacancy URL")
        asyncio.run(login(args.url))
    else:
        result = asyncio.run(prepare_workday_drafts())
        print(json.dumps(result))
        raise SystemExit(2 if result.get("source_errors") else 0)
