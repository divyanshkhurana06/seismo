"""Read-only helpers against the public MediaWiki APIs (evidence the Analyst can cite)."""
from __future__ import annotations

import html
import re

import requests

from .config import USER_AGENT

_H = {"User-Agent": USER_AGENT}
TAG_RE = re.compile(r"<[^>]+>")


def read_diff(lang: str, rev: int, max_chars: int = 3500) -> str:
    """What changed in one revision, as plain text (deleted/added lines)."""
    try:
        r = requests.get(f"https://{lang}.wikipedia.org/w/api.php", params={
            "action": "compare", "fromrev": rev, "torelative": "prev", "prop": "diff|title|user|comment", "format": "json",
        }, headers=_H, timeout=12).json()
    except Exception as e:
        return f"(diff unavailable: {e})"
    cmp = r.get("compare")
    if not cmp:
        return f"(no diff: {r.get('error', {}).get('info', 'unknown')})"
    body = cmp.get("*") or cmp.get("body") or ""
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        dele = re.findall(r'<td class="diff-deletedline[^"]*"[^>]*>(.*?)</td>', row, re.S)
        add = re.findall(r'<td class="diff-addedline[^"]*"[^>]*>(.*?)</td>', row, re.S)
        for d in dele:
            t = html.unescape(TAG_RE.sub("", d)).strip()
            if t:
                out.append("- " + t)
        for a in add:
            t = html.unescape(TAG_RE.sub("", a)).strip()
            if t:
                out.append("+ " + t)
    text = "\n".join(out) or "(no textual change; formatting/markup only)"
    head = f"[{lang}] rev {rev} by {cmp.get('touser','?')} on '{cmp.get('totitle','?')}' — comment: {cmp.get('tocomment','')}\n"
    return head + (text[:max_chars] + ("…" if len(text) > max_chars else ""))


def page_summary(lang: str, title: str) -> str:
    try:
        r = requests.get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(title.replace(' ', '_'), safe='')}",
                         headers=_H, timeout=10).json()
    except Exception as e:
        return f"(summary unavailable: {e})"
    return f"{r.get('title','')} — {r.get('description','')}\n{r.get('extract','')[:1200]}"
