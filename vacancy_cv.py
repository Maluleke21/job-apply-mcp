"""Build a vacancy-specific first page from evidence in a text-based master PDF."""
import hashlib
import html
import os
import re
import tempfile
from pathlib import Path

import pymupdf


class CVError(ValueError):
    pass


def keywords(text):
    stop = {"with", "from", "that", "this", "have", "will", "work", "team",
            "years", "role", "skills", "experience", "required", "about", "your",
            "their", "which", "knowledge", "strong", "ability", "working", "and", "the"}
    return set(re.findall(r"\b[a-z][a-z0-9+#.\-]*", text.casefold())) - stop


def highlights(master, title, description):
    target = keywords(description)
    role = keywords(title)
    ranked = {}
    for page in master:
        for block in page.get_text("blocks", sort=True):
            if block[6] != 0:
                continue
            for part in re.split(r"(?:^|\n)\s*(?:[•●▪]|[-*]\s)\s*", block[4]):
                excerpt = " ".join(part.split())
                if not 40 <= len(excerpt) <= 400 or "@" in excerpt or "://" in excerpt:
                    continue
                terms = {word for word in keywords(excerpt) if len(word) > 2 or word in {"ai","bi","qa","c#","ml","js"}}
                score = len(terms & target) + 3 * len(terms & role)
                if score:
                    ranked[excerpt] = score
    return sorted(ranked, key=ranked.get, reverse=True)[:4]


def prepare_cv(root, cfg, key, title, description):
    master_path = root / cfg["cv"]
    if not master_path.is_file():
        raise CVError("The configured master CV is missing.")
    with pymupdf.open(master_path) as master:
        if not master.is_pdf or master.needs_pass or not len(master):
            raise CVError("Provide an unlocked master CV in PDF format.")
        excerpts = highlights(master, title, description)
        if not excerpts:
            raise CVError("No matching text could be extracted from the master CV. Check this vacancy and CV.")
        with pymupdf.open() as out:
            page = out.new_page(width=595, height=842)
            markup = ("<h1>" + html.escape(cfg["name"]) + "</h1><h2>Application for " +
                      html.escape(title) + "</h2><h3>Relevant experience and skills</h3>" +
                      "".join("<p>" + html.escape(s) + "</p>" for s in excerpts))
            free, _ = page.insert_htmlbox(pymupdf.Rect(48, 45, 547, 795), markup,
                                           css="*{font-family:sans-serif}p{font-size:11pt;line-height:1.4}",
                                           scale_low=.85)
            if free < 0:
                raise CVError("Tailored highlights exceed the page size.")
            out.insert_pdf(master)
            content = out.tobytes(garbage=4, deflate=True, no_new_id=True)
    if len(content) > 5_000_000:
        raise CVError("The tailored PDF exceeds the upload size limit.")
    folder = root / "workday-cvs" / hashlib.sha256(key.encode()).hexdigest()[:20]
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    dest = folder / ("CV-" + digest[:20] + ".pdf")
    with tempfile.NamedTemporaryFile(dir=folder, delete=False) as tmp:
        tmp.write(content)
    os.replace(tmp.name, dest)
    return dest, digest
