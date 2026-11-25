"""FirstRand review-only pilot. Place beside jobs.py, settings.json and your CV."""
import argparse
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
HOST = "firstrand.wd3.myworkdayjobs.com"
TARGET = "R55281"
URL = f"https://{HOST}/en-GB/FRB/details/AI-Engineer-1_{TARGET}"
STEPS = ("Autofill with Resume", "My Information", "My Experience",
         "Application Questions", "Voluntary Disclosures", "Review")
MCP = FastMCP("FirstRand review pilot")
engine = context = tab = None


async def visible_one(locator):
    visible = [item for item in await locator.all() if await item.is_visible()]
    return visible[0] if len(visible) == 1 else None


async def phase(page):
    # Navigation lists show all six labels. Only inspect current-step markers or headings.
    current = page.locator('[aria-current="step"]:visible, [data-automation-id="progressBarActiveStep"]:visible')
    for text in await current.all_inner_texts():
        for step in STEPS:
            if step.lower() in text.lower():
                return step
    for step in reversed(STEPS):
        if await visible_one(page.get_by_role("heading", name=step, exact=True)):
            return step
    return ""


def pilot_page(url):
    u = urlsplit(url)
    return (u.scheme == "https" and u.netloc == HOST and
            re.search(r"/FRB/(?:job|details)/", u.path) and
            re.search(r"_R55281(?:/|$)", u.path))


async def entry(page, name):
    for role in ("button", "link"):
        item = await visible_one(page.get_by_role(role, name=name, exact=True))
        if item:
            await item.click(timeout=4000)
            return True
    return False


async def set_field(row, label, values):
    control = await visible_one(row.get_by_label(re.compile(label, re.I)))
    if control is None:
        return False
    tag = await control.evaluate("e => e.tagName")
    if tag == "SELECT":
        for value in values:
            if value in [v.strip() for v in await control.locator("option").all_text_contents()]:
                await control.select_option(label=value)
                return True
    elif tag in ("INPUT", "TEXTAREA") and await control.is_editable():
        if await control.get_attribute("role") == "combobox":
            return False
        await control.fill(values[0])
        return await control.input_value() == values[0]
    return False


async def correct_experience(page):
    path = ROOT / "workday_profile.json"
    if not path.exists():
        return []
    profile = json.loads(path.read_text(encoding="utf-8-sig"))
    applied = []
    rows = page.locator('[data-automation-id^="workExperience-"]')
    for row in await rows.all():
        titles = await row.get_by_label(re.compile(r"job title", re.I)).all()
        if len(titles) == 1 and (await titles[0].input_value()).strip().casefold() == profile["job_title"].casefold():
            company = await visible_one(row.get_by_label(re.compile("company", re.I)))
            if company and not (await company.input_value()).strip():
                if await set_field(row, "company", [profile["company"]]):
                    applied.append("Company name")
    edu = page.locator('[data-automation-id^="education-"]:visible')
    if await edu.count() == 1:
        fields = [(r"school|university|institution", [profile["institution"]]),
                  (r"^degree", profile["degree_labels"]),
                  (r"field of study", [profile["field_of_study"]]),
                  (r"^(from|start year)\b", [profile["start_year"]]), (r"^(to|end year)\b", [profile["end_year"]])]
        for label, values in fields:
            if await set_field(edu, label, values):
                applied.append(values[0] if len(values) == 1 else "Diploma selection")
    for row in await page.locator('[data-automation-id^="language-"]:visible').all():
        name = await visible_one(row.get_by_label(re.compile(r"^language\s*\*?$", re.I)))
        if name is None:
            continue
        selected = await name.evaluate("e => e.selectedOptions?.[0]?.text || e.value || e.innerText")
        if selected.strip().lower() != profile["language"].lower():
            continue
        fluent = await visible_one(row.get_by_role("checkbox", name=re.compile("fluent", re.I)))
        if fluent and profile["fluent"]:
            await fluent.check()
            applied.append("Language fluency")
        for label in ("reading", "speaking", "writing", "listening", "comprehension"):
            if await set_field(row, label, [profile["proficiency"]]):
                applied.append(profile["language"] + " " + label)
    return applied


async def assist(page):
    if not pilot_page(page.url):
        return "Paused. Sign in locally or return to FirstRand requisition R55281 in this tab."
    duplicate = page.get_by_text(re.compile(r"^(already applied|you (have |have already |already )applied( for (this|the) (job|position))?[.!]?)$", re.I))
    if any([await e.is_visible() for e in await duplicate.all()]):
        return "Already applied. Stop."
    step = await phase(page)
    if step == "Review" or await visible_one(page.get_by_role("button", name="Submit", exact=True)):
        return "Review reached. Stop here."
    if step == STEPS[0]:
        cv = ROOT / json.loads((ROOT / "settings.json").read_text(encoding="utf-8-sig"))["cv"]
        if await visible_one(page.get_by_text(cv.name, exact=True)):
            return "CV already present. Check the filename, then click Continue locally."
        fields = page.locator('input[type="file"]')
        if await fields.count() != 1:
            return "Upload control ambiguous. Use Select file locally, then Continue."
        if await fields.evaluate("e => e.files.length"):
            return "An attachment is already selected. Verify it and click Continue locally."
        if cv.stat().st_size > 5_000_000 or cv.read_bytes()[:5] != b"%PDF-":
            return "Check the configured PDF and its 5 MB size limit."
        await fields.set_input_files(str(cv))
        return "CV selected. Confirm the filename on screen, then click Continue locally."
    if step == "My Experience":
        applied = await correct_experience(page)
        return ("Updated recognised controls: " + (", ".join(applied) or "none") +
                ". Verify employment, education and language entries locally, then Continue.")
    if step in STEPS:
        return f"{step}: review the answers locally and choose Continue when ready."
    if await entry(page, "Continue Application"):
        return "Resumed your existing draft. Inspect the current step."
    if await entry(page, "Autofill with Resume"):
        return "Selected resume autofill. Inspect the current step."
    return "Inspect the page. If a draft exists, resume it. Otherwise choose Apply locally."


@MCP.tool()
async def open_firstrand_pilot() -> str:
    """Open FirstRand AI Engineer-1 in a local visible browser. No application is submitted."""
    global engine, context, tab
    if tab is None or tab.is_closed():
        if context is not None:
            await close_pilot()
        engine = await async_playwright().start()
        try:
            context = await engine.chromium.launch_persistent_context(
                str(Path.home() / ".job-apply-firstrand-browser"), headless=False)
            tab = context.pages[0] if context.pages else await context.new_page()
            await tab.goto(URL, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            await close_pilot()
            raise
    return "Pilot open. Sign in locally and check for an existing draft or prior application."


@MCP.tool()
async def assist_current_step() -> str:
    """Inspect the current step, upload the configured PDF or correct supported controls. Stop at Review."""
    if tab is None or tab.is_closed():
        return "Open the pilot first."
    return await assist(tab)


@MCP.tool()
async def close_pilot() -> str:
    """Close the pilot browser."""
    global engine, context, tab
    try:
        if context:
            await context.close()
    finally:
        if engine:
            await engine.stop()
        engine = context = tab = None
    return "Pilot closed."


async def interactive():
    try:
        print(await open_firstrand_pilot())
        while True:
            cmd = await asyncio.to_thread(input, "Enter to assist the visible step, or q to close: ")
            if cmd.strip().lower() == "q" or tab.is_closed():
                break
            print(await assist_current_step())
    finally:
        await close_pilot()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["pilot", "server"])
    args = parser.parse_args()
    if args.mode == "server":
        MCP.run(transport="stdio")
    else:
        asyncio.run(interactive())
