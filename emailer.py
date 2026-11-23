"""Weekly email digest of new job matches.

Settings live in email.json (gitignored). Two ways to send:
  - "mail":  the Mac Mail app, using whatever account is set up there (no password stored here)
  - "gmail": Gmail over SMTP with an app password, which is kept in the macOS Keychain, never in a file
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import smtplib
import ssl
import subprocess
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF = HERE / "email.json"
KEYCHAIN_SERVICE = "nyc-job-scout-gmail"
DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]  # launchd Weekday 0-6
DEFAULTS = {"enabled": False, "to": "", "day": "Saturday", "time": "09:00", "method": "mail",
            "gmail_user": "", "min_score": 60}
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


def load_conf() -> dict:
    try:
        return {**DEFAULTS, **json.loads(CONF.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_conf(conf: dict):
    CONF.write_text(json.dumps({k: conf.get(k, v) for k, v in DEFAULTS.items()}, indent=2))


def validate(conf: dict):
    if not EMAIL_RE.fullmatch(conf.get("to", "").strip()):
        raise ValueError("Enter the email address the weekly update should go to.")
    if conf.get("day") not in DAYS:
        raise ValueError("Pick a day.")
    if conf.get("method") == "gmail":
        if not EMAIL_RE.fullmatch(conf.get("gmail_user", "").strip()):
            raise ValueError("Enter the Gmail address to send from.")
        if not has_password(conf["gmail_user"]):
            raise ValueError("Add a Gmail app password (see the link under the field).")


# ---- Keychain ---------------------------------------------------------------
def save_password(user: str, password: str):
    password = re.sub(r"\s+", "", password)
    r = subprocess.run(["/usr/bin/security", "add-generic-password", "-U", "-a", user, "-s", KEYCHAIN_SERVICE,
                        "-w", password], capture_output=True, text=True)
    if r.returncode:
        raise ValueError(f"Couldn't save the password to Keychain: {r.stderr.strip()}")


def get_password(user: str) -> str | None:
    r = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", user, "-s", KEYCHAIN_SERVICE, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def has_password(user: str) -> bool:
    return bool(user) and get_password(user) is not None


# ---- Digest -----------------------------------------------------------------
def build_digest(jobs: list[dict], min_score: int, days: int = 7, limit: int = 30):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    fresh = [j for j in jobs if (j.get("first_seen") or "") >= since and j["final_score"] >= min_score]
    fresh.sort(key=lambda j: j["final_score"], reverse=True)
    top = fresh[:limit]
    week = f"{dt.date.today() - dt.timedelta(days=days):%b %-d} to {dt.date.today():%b %-d}"
    subject = (f"NYC Job Scout: {len(fresh)} new match{'es' if len(fresh) != 1 else ''} this week"
               if fresh else "NYC Job Scout: no new matches this week")
    head = (f"{len(fresh)} postings first seen {week} scored {min_score}+."
            + (f" Here are the top {len(top)}." if len(fresh) > len(top) else ""))
    if not fresh:
        head = f"No new postings scored {min_score}+ between {week}. Your full results are at http://127.0.0.1:8765"

    lines = [head, ""]
    for j in top:
        lines += [f"{j['final_score']}  {j['title']} at {j['company']} ({j['location']})",
                  f"     fit {j['fit']} · company outlook {j['outlook']}" + (f" · {j['salary']}" if j.get("salary") else ""),
                  f"     {j['url']}", ""]
    text = "\n".join(lines)

    e = html.escape
    col = lambda s: "#1f8a4c" if s >= 75 else "#b7791f" if s >= 55 else "#b33a3a"
    rows = "".join(
        f'<tr><td style="padding:8px 10px;font:700 18px sans-serif;color:{col(j["final_score"])};vertical-align:top">'
        f'{j["final_score"]}</td><td style="padding:8px 0;font:14px/1.4 sans-serif">'
        f'<a href="{e(j["url"])}" style="color:#2457d6;font-weight:600;text-decoration:none">{e(j["title"])}</a><br>'
        f'<span style="color:#555">{e(j["company"])} · {e(j["location"])}'
        f'{" · " + e(j["salary"]) if j.get("salary") else ""}</span><br>'
        f'<span style="color:#888;font-size:12px">Job fit {j["fit"]} · company outlook {j["outlook"]}'
        f'{" · skills: " + e(", ".join((j.get("matched") or [])[:6])) if j.get("matched") else ""}</span></td></tr>'
        for j in top)
    body = (f'<div style="max-width:640px;margin:0 auto;padding:16px;font:14px sans-serif;color:#1c1c1e">'
            f'<h2 style="margin:0 0 6px;font-size:18px">Your weekly NYC job matches</h2>'
            f'<p style="color:#555;margin:0 0 12px">{e(head)}</p><table style="border-collapse:collapse">{rows}</table>'
            f'<p style="color:#888;font-size:12px;margin-top:16px">Sent by NYC Job Scout on your Mac. '
            f'Change or stop this email at http://127.0.0.1:8765</p></div>')
    return subject, text, body, len(fresh)


# ---- Sending ----------------------------------------------------------------
MAIL_SCRIPT = """on run argv
  tell application "Mail"
    set m to make new outgoing message with properties {subject:item 1 of argv, content:item 2 of argv, visible:false}
    tell m to make new to recipient at end of to recipients with properties {address:item 3 of argv}
    send m
  end tell
end run"""


def send(conf: dict, subject: str, text: str, html_body: str):
    to = conf["to"].strip()
    if conf.get("method") == "gmail":
        user = conf["gmail_user"].strip()
        password = get_password(user)
        if not password:
            raise ValueError("No Gmail app password saved in Keychain.")
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, user, to
        msg.set_content(text)
        msg.add_alternative(html_body, subtype="html")
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
                s.login(user, password)
                s.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            raise ValueError("Gmail rejected the login. Use an app password, not your normal password.")
        except (smtplib.SMTPException, OSError) as e:
            raise ValueError(f"Gmail sending failed: {e}")
    else:
        r = subprocess.run(["/usr/bin/osascript", "-e", MAIL_SCRIPT, subject, text, to],
                           capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise ValueError("The Mail app couldn't send it. Make sure Mail has an account set up and that "
                             f"NYC Job Scout is allowed to control Mail (System Settings > Privacy & Security > "
                             f"Automation). Details: {r.stderr.strip()}")
