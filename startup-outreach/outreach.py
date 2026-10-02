#!/usr/bin/env python3
"""Personalized cold emails to startups, sent only after you approve each one.

For every company in your spreadsheet it:
  1. researches the company on the web (Claude API + web search),
  2. drafts a short, plain email that ties your resume to what they build,
  3. shows you the draft and waits: send / edit / regenerate / skip / quit,
  4. sends approved emails from your Gmail with your resume attached,
  5. logs every send so a company is never emailed twice.

Usage:
  python outreach.py                 # review and send, one company at a time
  python outreach.py --dry-run       # draft only, never send
  python outreach.py --limit 5       # stop after 5 companies this session
  python outreach.py --only "Razorpay"
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import getpass
import json
import os
import re
import smtplib
import subprocess
import sys
import tempfile
import time
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

import anthropic

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
SENT_LOG = HERE / "sent_log.csv"
DRAFTS_DIR = HERE / "drafts"
RESEARCH_DIR = DRAFTS_DIR / "research"

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

GENERIC_INBOXES = {"info", "contact", "hello", "support", "admin", "sales", "team", "office", "hi"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Spreadsheet header aliases -> canonical field.
COLUMN_ALIASES = {
    "company": ["company", "company name", "startup", "startup name", "organisation", "organization", "name"],
    "email": ["email", "email id", "email address", "mail", "e-mail", "contact email"],
    "contact_name": ["contact name", "contact", "founder", "founder name", "person", "poc", "hr name", "recipient"],
    "website": ["website", "url", "site", "domain", "link", "company website"],
    "notes": ["notes", "note", "comments", "remarks", "role", "description"],
}

RESEARCH_SYSTEM = """You research early-stage companies for a job seeker writing a short cold email.
Use web search to find out what the company actually does. Prefer the company's own site, recent
news, funding announcements, their careers page, engineering blog, and founder posts.

Return a brief in plain text with these headings:
WHAT THEY BUILD: one or two sentences, concrete (product, customers, problem).
STAGE: funding stage, notable investors, team size, if found.
RECENT: one or two recent, dated things (launch, raise, hire, blog post), if found.
TECH / ROLES: tech stack hints and any open roles, especially engineering, if found.
BEST HOOK: the single most specific thing a candidate could genuinely reference.
CONFIDENCE: high / medium / low, depending on how sure you are this is the right company.

Never guess. If something is not found, write "not found". If the name is ambiguous, say which
company you think it is and why."""

WRITER_SYSTEM = """You write cold emails from a job seeker to a startup. The emails must read like
a real person wrote them quickly and carefully, not like a template or a marketing email.

Rules:
- 90 to 150 words in the body. Plain text. Short paragraphs. No bullet lists, no bold, no emojis.
- Open with the person's first name if known ("Hi Priya,"), otherwise "Hi <Company> team,".
- First line after the greeting says something specific and true about what they build or did
  recently, taken only from the research brief. One sentence, not flattery.
- Then two or three sentences on the candidate's most relevant work for THIS company, taken only
  from the resume. Name concrete projects, numbers and tools that actually appear in the resume.
- Mention the website once, naturally (for example "More of my work is at <url>.").
- Say the resume is attached. End with one low-pressure ask (a short call, or who to talk to).
- Sign off with just the candidate's first name and full name / phone on the next line if given.
- Subject line: under 8 words, specific to the company, lowercase-ish and casual is fine.
  No "Application for", no "Opportunity", no exclamation marks.

Never use: "I hope this email finds you well", "I came across", "I am writing to", "passionate",
"synergy", "leverage", "cutting-edge", "revolutionize", "excited to", "thrilled", "esteemed",
"game-changer", "delve", em dashes. Do not over-praise the company.

Never invent anything. Every claim about the candidate must be in the resume or the extra notes.
Every claim about the company must be in the research brief. If the brief has low confidence or
little detail, keep the company part short and general rather than making something up, and say
so in the "warnings" field."""

EMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "personalization": {
            "type": "string",
            "description": "One line: which company fact and which resume item the email uses.",
        },
        "warnings": {
            "type": "string",
            "description": "Anything the sender should double-check, or an empty string.",
        },
    },
    "required": ["subject", "body", "personalization", "warnings"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- config and data


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing {CONFIG_PATH.name}. Copy config.example.json to config.json and fill it in.")
    cfg = json.loads(CONFIG_PATH.read_text())
    for key in ("your_name", "your_email", "resume_path", "website", "companies_file"):
        if not cfg.get(key):
            sys.exit(f"config.json: '{key}' is required.")
    cfg["resume_path"] = str((HERE / cfg["resume_path"]).resolve()) if not os.path.isabs(cfg["resume_path"]) else cfg["resume_path"]
    if not Path(cfg["resume_path"]).exists():
        sys.exit(f"Resume not found at {cfg['resume_path']}")
    cfg.setdefault("daily_limit", 25)
    cfg.setdefault("min_seconds_between_sends", 60)
    return cfg


def _canonical(header: str) -> str | None:
    h = header.strip().lower().replace("_", " ")
    for field, aliases in COLUMN_ALIASES.items():
        if h in aliases:
            return field
    return None


def load_companies(path: str) -> list[dict]:
    p = Path(path) if os.path.isabs(path) else HERE / path
    if not p.exists():
        sys.exit(f"Companies file not found: {p}")

    if p.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            sys.exit("Reading .xlsx needs openpyxl: pip install openpyxl (or save the sheet as CSV).")
        ws = openpyxl.load_workbook(p, read_only=True, data_only=True).active
        rows = list(ws.iter_rows(values_only=True))
        headers, data = [str(h or "") for h in rows[0]], rows[1:]
        raw = [dict(zip(headers, ["" if v is None else str(v) for v in r])) for r in data]
    else:
        with open(p, newline="", encoding="utf-8-sig") as f:
            raw = list(csv.DictReader(f))

    companies = []
    for row in raw:
        rec = {}
        for header, value in row.items():
            field = _canonical(header or "")
            if field and field not in rec and value and str(value).strip():
                rec[field] = str(value).strip()
        if rec.get("company") and rec.get("email"):
            companies.append(rec)
    if not companies:
        sys.exit("No rows with both a company name and an email. Check the column headers (see companies.example.csv).")
    return companies


def load_sent() -> list[dict]:
    if not SENT_LOG.exists():
        return []
    with open(SENT_LOG, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def log_sent(company: dict, subject: str) -> None:
    new = not SENT_LOG.exists()
    with open(SENT_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["sent_at", "company", "email", "subject"])
        w.writerow([dt.datetime.now().isoformat(timespec="seconds"), company["company"], company["email"], subject])


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "company"


# --------------------------------------------------------------------------- Claude calls


def _text_of(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text").strip()


def _check_refusal(response, what: str) -> None:
    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "explanation", None) if response.stop_details else None
        raise RuntimeError(f"Claude declined the {what} request. {detail or ''}".strip())


def research_company(client: anthropic.Anthropic, company: dict, city: str) -> str:
    cache = RESEARCH_DIR / f"{slug(company['company'])}.md"
    if cache.exists():
        return cache.read_text()

    known = [f"Company: {company['company']}", f"Location: {city}"]
    if company.get("website"):
        known.append(f"Website: {company['website']}")
    if company.get("notes"):
        known.append(f"Notes from the sender's spreadsheet: {company['notes']}")
    user_msg = "\n".join(known) + "\n\nResearch this company and write the brief."

    messages = [{"role": "user", "content": user_msg}]
    for _ in range(5):  # resume server-side tool loops that pause
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            system=RESEARCH_SYSTEM,
            output_config={"effort": "medium"},
            tools=[
                {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3},
            ],
            messages=messages,
        )
        _check_refusal(response, "research")
        if response.stop_reason != "pause_turn":
            break
        messages = [{"role": "user", "content": user_msg}, {"role": "assistant", "content": response.content}]

    brief = _text_of(response) or "No information found."
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(brief)
    return brief


def draft_email(client: anthropic.Anthropic, cfg: dict, company: dict, brief: str,
                resume_b64: str, feedback: str | None = None, previous: dict | None = None) -> dict:
    about_me = [
        f"Candidate name: {cfg['your_name']}",
        f"Website: {cfg['website']}",
    ]
    if cfg.get("phone"):
        about_me.append(f"Phone: {cfg['phone']}")
    if cfg.get("target_role"):
        about_me.append(f"Looking for: {cfg['target_role']}")
    if cfg.get("extra_notes"):
        about_me.append(f"Extra notes from the candidate: {cfg['extra_notes']}")

    recipient = [f"Company: {company['company']}", f"Recipient email: {company['email']}"]
    recipient.append(f"Recipient name: {company.get('contact_name') or 'unknown'}")

    request = (
        "\n".join(recipient)
        + "\n\nResearch brief:\n" + brief
        + "\n\nWrite the email."
    )
    if previous and feedback:
        request += (
            "\n\nYour previous draft was:\nSubject: " + previous["subject"] + "\n\n" + previous["body"]
            + "\n\nRewrite it with this feedback from the candidate: " + feedback
        )

    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        system=WRITER_SYSTEM,
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": EMAIL_SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                # Resume + candidate info are identical for every company, so they are cached.
                {"type": "document", "title": "Candidate resume",
                 "source": {"type": "base64", "media_type": "application/pdf", "data": resume_b64}},
                {"type": "text", "text": "\n".join(about_me), "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": request},
            ],
        }],
    )
    _check_refusal(response, "drafting")
    return json.loads(_text_of(response))


# --------------------------------------------------------------------------- review and send


def edit_in_editor(subject: str, body: str) -> tuple[str, str]:
    text = f"Subject: {subject}\n\n{body}\n"
    editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "nano")
    try:
        if not sys.stdin.isatty() or "google.colab" in sys.modules:
            raise OSError("no terminal editor here")
        with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(text)
            path = f.name
        subprocess.run([editor, path], check=True)
        text = Path(path).read_text(encoding="utf-8")
        os.unlink(path)
    except (OSError, subprocess.CalledProcessError):
        print("\nNo editor available. Paste the full email below (first line 'Subject: ...'),")
        print("then a line containing only a single dot '.' to finish:")
        lines = []
        while (line := input()) != ".":
            lines.append(line)
        text = "\n".join(lines)

    first, _, rest = text.partition("\n")
    if first.lower().startswith("subject:"):
        return first.split(":", 1)[1].strip(), rest.strip()
    return subject, text.strip()


def build_message(cfg: dict, company: dict, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((cfg["your_name"], cfg["your_email"]))
    msg["To"] = company["email"]
    msg["Subject"] = subject
    msg.set_content(body)
    resume = Path(cfg["resume_path"])
    attach_name = cfg.get("resume_attachment_name") or resume.name
    msg.add_attachment(resume.read_bytes(), maintype="application", subtype="pdf", filename=attach_name)
    return msg


class GmailSender:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.password = os.environ.get("GMAIL_APP_PASSWORD") or getpass.getpass(
            f"Gmail app password for {cfg['your_email']} (input hidden): ")
        self.password = self.password.replace(" ", "")
        self.last_sent = 0.0

    def send(self, msg: EmailMessage) -> None:
        wait = self.cfg["min_seconds_between_sends"] - (time.time() - self.last_sent)
        if wait > 0:
            print(f"  waiting {int(wait)}s before sending (spacing out sends)...")
            time.sleep(wait)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(self.cfg["your_email"], self.password)
            smtp.send_message(msg)
        self.last_sent = time.time()


def show_draft(company: dict, email: dict, attach_name: str) -> None:
    bar = "=" * 72
    print(f"\n{bar}\nTo:      {company['email']}  ({company['company']})")
    print(f"Subject: {email['subject']}\nAttach:  {attach_name}\n{'-' * 72}")
    print(email["body"])
    print("-" * 72)
    if email.get("personalization"):
        print(f"Uses:    {email['personalization']}")
    if email.get("warnings"):
        print(f"CHECK:   {email['warnings']}")
    print(bar)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="draft and save emails, never send")
    ap.add_argument("--limit", type=int, default=None, help="max companies to process this session")
    ap.add_argument("--only", default=None, help="process only companies whose name contains this text")
    args = ap.parse_args()

    cfg = load_config()
    companies = load_companies(cfg["companies_file"])
    sent = load_sent()
    sent_emails = {r["email"].strip().lower() for r in sent}
    sent_companies = {r["company"].strip().lower() for r in sent}
    today = dt.date.today().isoformat()
    sent_today = sum(1 for r in sent if r["sent_at"].startswith(today))

    todo = [c for c in companies
            if c["email"].lower() not in sent_emails and c["company"].lower() not in sent_companies]
    if args.only:
        todo = [c for c in todo if args.only.lower() in c["company"].lower()]
    if args.limit:
        todo = todo[: args.limit]

    print(f"{len(companies)} companies in sheet, {len(companies) - len(todo)} skipped (already emailed or filtered), "
          f"{len(todo)} to go. Sent today: {sent_today}/{cfg['daily_limit']}.")
    if not todo:
        return

    client = anthropic.Anthropic()
    resume_b64 = base64.standard_b64encode(Path(cfg["resume_path"]).read_bytes()).decode()
    attach_name = cfg.get("resume_attachment_name") or Path(cfg["resume_path"]).name
    sender = None if args.dry_run else GmailSender(cfg)
    city = cfg.get("city", "Bengaluru, India")
    DRAFTS_DIR.mkdir(exist_ok=True)

    for i, company in enumerate(todo, 1):
        if not args.dry_run and sent_today >= cfg["daily_limit"]:
            print(f"\nDaily limit of {cfg['daily_limit']} reached. Run again tomorrow.")
            break

        name, addr = company["company"], company["email"]
        print(f"\n[{i}/{len(todo)}] {name} <{addr}>")
        if not EMAIL_RE.match(addr):
            print("  skipping: email address looks invalid")
            continue
        if addr.split("@")[0].lower() in GENERIC_INBOXES:
            print("  note: generic inbox, a founder's or recruiter's address usually works better")

        try:
            print("  researching...")
            brief = research_company(client, company, city)
            print("  drafting...")
            email = draft_email(client, cfg, company, brief, resume_b64)
        except (anthropic.APIError, RuntimeError, json.JSONDecodeError) as e:
            print(f"  failed: {e}")
            continue

        while True:
            show_draft(company, email, attach_name)
            draft_file = DRAFTS_DIR / f"{slug(name)}.txt"
            draft_file.write_text(f"To: {addr}\nSubject: {email['subject']}\n\n{email['body']}\n", encoding="utf-8")

            if args.dry_run:
                print(f"  saved draft to {draft_file.relative_to(HERE)}")
                break

            choice = input("[y] send  [e] edit  [r] rewrite with feedback  [b] show research  [s] skip  [q] quit > ").strip().lower()
            if choice == "y":
                try:
                    sender.send(build_message(cfg, company, email["subject"], email["body"]))
                except (smtplib.SMTPException, OSError) as e:
                    print(f"  send failed: {e}")
                    if isinstance(e, smtplib.SMTPAuthenticationError):
                        sys.exit("Gmail rejected the login. Check your_email and the app password (see README).")
                    continue
                log_sent(company, email["subject"])
                sent_today += 1
                print(f"  sent. ({sent_today}/{cfg['daily_limit']} today)")
                break
            if choice == "e":
                email["subject"], email["body"] = edit_in_editor(email["subject"], email["body"])
                email["warnings"] = ""
            elif choice == "r":
                feedback = input("What should change? > ").strip()
                if feedback:
                    try:
                        email = draft_email(client, cfg, company, brief, resume_b64, feedback, email)
                    except (anthropic.APIError, RuntimeError, json.JSONDecodeError) as e:
                        print(f"  rewrite failed: {e}")
            elif choice == "b":
                print("\n" + brief)
            elif choice == "s":
                print("  skipped.")
                break
            elif choice == "q":
                print("Stopped. Nothing else was sent.")
                return

    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Only emails you approved with [y] were sent.")
