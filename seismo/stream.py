"""Wikimedia EventStreams consumer: live SSE, JSONL recording, and timed replay."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from typing import Iterator

import requests

from .config import KEEP_TYPES, STREAM_URL, USER_AGENT

IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$|^[0-9a-f:]{6,}$", re.I)
REVERT_RE = re.compile(r"\b(revert|reverted|undid|undo|rv|rvv|rollback)\b", re.I)


def normalize(raw: dict) -> dict | None:
    """Reduce a recentchange event to what the graph needs. Returns None if not interesting."""
    if raw.get("type") not in KEEP_TYPES or raw.get("namespace") != 0:
        return None
    domain = raw.get("meta", {}).get("domain", "")
    if not domain.endswith(".wikipedia.org") or raw.get("bot"):
        return None
    user = raw.get("user") or "?"
    anon = bool(IP_RE.match(user))
    # Anonymous editors are identified by IP; keep only a salted hash so no IP enters the graph.
    editor = ("anon:" + hashlib.sha1(("seismo|" + user).encode()).hexdigest()[:10]) if anon else user
    length = raw.get("length") or {}
    rev = raw.get("revision") or {}
    comment = raw.get("comment") or ""
    return {
        "wiki": raw.get("wiki", domain.split(".")[0] + "wiki"),
        "lang": domain.split(".")[0],
        "title": raw.get("title", ""),
        "url": raw.get("title_url") or raw.get("meta", {}).get("uri", ""),
        "editor": editor,
        "anon": anon,
        "ts": int(raw.get("timestamp") or time.time()),
        "rev": int(rev.get("new") or 0),
        "old_rev": int(rev.get("old") or 0),
        "delta": int((length.get("new") or 0) - (length.get("old") or 0)),
        "comment": comment[:240],
        "revert": bool(REVERT_RE.search(comment)),
        "new_page": raw.get("type") == "new",
        "minor": bool(raw.get("minor")),
        "diff_url": raw.get("notify_url") or "",
    }


def live_events() -> Iterator[dict]:
    """Yield normalized events from the live stream forever, reconnecting on failure."""
    backoff = 1
    while True:
        try:
            with requests.get(STREAM_URL, headers={"User-Agent": USER_AGENT, "Accept": "text/event-stream"},
                              stream=True, timeout=(10, 90)) as r:
                r.raise_for_status()
                backoff = 1
                for line in r.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    try:
                        ev = normalize(json.loads(line[5:].strip()))
                    except Exception:
                        continue
                    if ev:
                        yield ev
        except Exception as e:  # network hiccup: reconnect
            print(f"[stream] reconnecting after error: {e}", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


def record(path: str) -> None:
    with open(path, "a") as f:
        for i, ev in enumerate(live_events()):
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            if i % 200 == 0:
                f.flush()
                print(f"[record] {i} events", file=sys.stderr)


def replay(path: str, speed: float = 10.0, rebase: bool = True) -> Iterator[dict]:
    """Replay a recording, pacing by original timestamps / speed. rebase shifts timestamps to 'now'."""
    events = [json.loads(l) for l in open(path) if l.strip()]
    if not events:
        return
    t0 = events[0]["ts"]
    start = time.time()
    for ev in events:
        due = start + (ev["ts"] - t0) / speed
        delay = due - time.time()
        if delay > 0:
            time.sleep(min(delay, 5))
        ev = dict(ev)
        if rebase:  # compress the original timeline onto wall-clock time
            ev["ts"] = int(due)
        yield ev


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "record":
        record(sys.argv[2])
    else:
        for ev in live_events():
            print(json.dumps(ev, ensure_ascii=False))
