"""Notifications: a GitHub issue comment (GitHub emails it to you) and,
optionally, WhatsApp via CallMeBot. Every channel is best-effort: a failed
notification never stops the bot."""
from __future__ import annotations

import os
from urllib.parse import quote

import requests

ISSUE_TITLE = "Littlefield bot updates"


def _gh(method, path, **kw):
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None
    r = requests.request(method, f"https://api.github.com/repos/{repo}{path}", timeout=20,
                         headers={"Authorization": f"Bearer {token}",
                                  "Accept": "application/vnd.github+json"}, **kw)
    r.raise_for_status()
    return r.json()


def github_issue(text: str, state: dict, owner: str | None = None) -> str:
    num = state.get("notify_issue")
    if not num:
        for it in _gh("GET", "/issues", params={"state": "open", "per_page": 50}) or []:
            if it.get("title") == ISSUE_TITLE:
                num = it["number"]
                break
    if not num:
        body = "The bot comments here after every run that changes something in the game."
        if owner:
            body += f"\n\ncc @{owner}"   # mention so GitHub subscribes you and emails each comment
        made = _gh("POST", "/issues", json={"title": ISSUE_TITLE, "body": body})
        if not made:
            return "github: no token"
        num = made["number"]
    state["notify_issue"] = num
    _gh("POST", f"/issues/{num}/comments", json={"body": text})
    return f"github issue #{num}"


def whatsapp(text: str) -> str:
    phone, key = os.environ.get("WA_PHONE"), os.environ.get("WA_APIKEY")
    if not phone or not key:
        return "whatsapp: not set up"
    r = requests.get("https://api.callmebot.com/whatsapp.php",
                     params={"phone": phone, "text": text[:1500], "apikey": key}, timeout=20)
    return f"whatsapp {r.status_code}"


def send(text: str, state: dict, owner: str | None = None) -> list[str]:
    out = []
    for fn in (lambda: github_issue(text, state, owner), lambda: whatsapp(text)):
        try:
            out.append(fn())
        except Exception as e:  # noqa: BLE001
            out.append(f"notify failed: {type(e).__name__}")
    return out
