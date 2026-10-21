"""FastAPI app: ingest thread + detector/scout scheduler + analyst worker + SSE fan-out + Desk chat."""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import signals
from .config import WINDOW_SECONDS  # noqa: F401  (importing config also loads .env)
from .store import GraphWriter, ensure_schema, graph
from .stream import live_events, replay

STATIC = Path(__file__).resolve().parent.parent / "static"
SOURCE = os.environ.get("SEISMO_SOURCE", "live")          # live | replay:<path>[:speed]
DETECT_EVERY = int(os.environ.get("SEISMO_DETECT_EVERY", 45))
AGENTS_ON = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

app = FastAPI(title="Seismo")
state = {"tremors": None, "started": int(time.time()), "ingested": 0, "log": [], "agents": AGENTS_ON, "source": SOURCE}


# ---------------------------------------------------------------- SSE broadcaster
class Broadcaster:
    def __init__(self):
        self.clients: list[queue.Queue] = []
        self.lock = threading.Lock()

    def emit(self, ev: dict):
        ev.setdefault("ts", time.time())
        if ev.get("type") in ("agent_log", "task", "brief"):
            state["log"].append(ev)
            del state["log"][:-200]
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    pass

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)


bus = Broadcaster()


def sse(q: queue.Queue, heartbeat: float = 15.0):
    last = time.time()
    while True:
        try:
            ev = q.get(timeout=1.0)
            yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
            last = time.time()
        except queue.Empty:
            if time.time() - last > heartbeat:
                yield ": ping\n\n"
                last = time.time()


# ---------------------------------------------------------------- background threads
def ingest_thread():
    ensure_schema()
    w = GraphWriter()

    def on_rows(rows):
        state["ingested"] = w.total
        bus.emit({"type": "edits", "rows": [{"lang": r["lang"], "title": r["title"], "editor": r["editor"], "anon": r["anon"],
                                              "delta": r["delta"], "revert": r["revert"], "key": r["wiki"] + "|" + r["title"],
                                              "rev": r["rev"], "ts": r["ts"]} for r in rows]})
    w.listeners.append(on_rows)

    def prune_loop():
        while True:
            time.sleep(120)
            try:
                w.prune()
            except Exception as e:
                print(f"[prune] {e}", flush=True)
    threading.Thread(target=prune_loop, daemon=True, name="prune").start()
    if SOURCE.startswith("replay:"):
        parts = SOURCE.split(":")
        while True:  # loop the recording forever
            w.run(replay(parts[1], speed=float(parts[2]) if len(parts) > 2 else 20.0))
    else:
        w.run(live_events())


def scheduler_thread():
    from . import agents
    time.sleep(20)
    while True:
        try:
            d = signals.detect()
            try:
                signals.link_same_as(d["pages"])
            except Exception:
                pass
            state["tremors"] = d
            bus.emit({"type": "tremors", "components": d["components"][:10], "n_pages": len(d["pages"]), "since": d["since"]})
            if AGENTS_ON and d["components"]:
                agents.scout(d["components"][:5], bus.emit)
            elif not AGENTS_ON:
                bus.emit({"type": "agent_log", "agent": "system", "msg": "agents offline: set ANTHROPIC_API_KEY to enable Scout/Analyst/Desk"})
        except Exception as e:
            bus.emit({"type": "agent_log", "agent": "system", "msg": f"detector error: {e}"})
        time.sleep(DETECT_EVERY)


def analyst_thread(worker: str):
    from . import agents
    time.sleep(30)
    while True:
        task = None
        try:
            task = agents.claim_task(worker)
            if task:
                agents.analyst(task, bus.emit, worker=worker)
        except Exception as e:
            bus.emit({"type": "agent_log", "agent": worker, "msg": f"error: {e}"})
        time.sleep(2 if task else 6)


@app.on_event("startup")
def start():
    threading.Thread(target=ingest_thread, daemon=True, name="ingest").start()
    threading.Thread(target=scheduler_thread, daemon=True, name="scheduler").start()
    if AGENTS_ON:
        for i in range(int(os.environ.get("SEISMO_ANALYSTS", 2))):
            threading.Thread(target=analyst_thread, args=(f"analyst-{i+1}",), daemon=True).start()


# ---------------------------------------------------------------- API
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/events")
def events():
    q = bus.subscribe()
    q.put({"type": "hello", "agents": AGENTS_ON, "source": SOURCE, "log": state["log"][-60:]})
    return StreamingResponse(sse(q), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/stats")
def stats():
    g = graph()
    counts = {r[0]: r[1] for r in g.ro_query("MATCH (n) RETURN labels(n)[0] AS l, count(n)").result_set}
    rels = {r[0]: r[1] for r in g.ro_query("MATCH ()-[r]->() RETURN type(r), count(r)").result_set}
    return {"nodes": counts, "rels": rels, "ingested": state["ingested"], "uptime": int(time.time()) - state["started"],
            "agents": AGENTS_ON, "source": SOURCE}


@app.get("/api/pulse")
def pulse(minutes: int = 60):
    since = int(time.time()) - minutes * 60
    rows = graph().ro_query("""MATCH (e:Edit) WHERE e.ts > $since
                               WITH (e.ts / 60) * 60 AS m, count(e) AS n, sum(CASE WHEN e.revert THEN 1 ELSE 0 END) AS rv
                               RETURN m, n, rv ORDER BY m""", params={"since": since}).result_set
    return {"since": since, "series": [{"m": m, "n": n, "reverts": rv} for m, n, rv in rows]}


@app.get("/api/tremors")
def tremors():
    return state["tremors"] or {"components": [], "pages": []}


@app.get("/api/graph")
def hot_graph(window: int = WINDOW_SECONDS, min_editors: int = 2, limit: int = 160):
    """Hot subgraph for the live visual: pages with >= min_editors in window, their editors, SAME_AS links, story links."""
    g = graph()
    since = int(time.time()) - window
    pages = g.ro_query("""MATCH (u:Editor)-[:MADE]->(e:Edit)-[:ON]->(p:Page) WHERE e.ts > $since
                          WITH p, count(DISTINCT u) AS editors, count(e) AS edits, sum(CASE WHEN e.revert THEN 1 ELSE 0 END) AS rv
                          WHERE editors >= $me
                          RETURN p.key, p.lang, p.title, p.url, editors, edits, rv, p.qid ORDER BY editors DESC, edits DESC LIMIT $lim""",
                       params={"since": since, "me": min_editors, "lim": limit}).result_set
    keys = [r[0] for r in pages]
    nodes = [{"id": r[0], "kind": "page", "lang": r[1], "title": r[2], "url": r[3], "editors": r[4], "edits": r[5],
              "reverts": r[6], "qid": r[7]} for r in pages]
    links = []
    if keys:
        eds = g.ro_query("""UNWIND $keys AS k MATCH (p:Page {key: k})<-[:ON]-(e:Edit)<-[:MADE]-(u:Editor) WHERE e.ts > $since
                            WITH u, p, count(e) AS n RETURN u.name, u.anon, p.key, n""",
                         params={"keys": keys, "since": since}).result_set
        seen = set()
        for name, anon, pk, n in eds:
            eid = "u:" + name
            if eid not in seen:
                seen.add(eid)
                nodes.append({"id": eid, "kind": "editor", "title": name, "anon": anon})
            links.append({"source": eid, "target": pk, "kind": "edit", "n": n})
        for a, b in g.ro_query("UNWIND $keys AS k MATCH (p:Page {key: k})-[:SAME_AS]-(q:Page) WHERE q.key IN $keys AND p.key < q.key RETURN p.key, q.key",
                               params={"keys": keys}).result_set:
            links.append({"source": a, "target": b, "kind": "same_as"})
        for sid, head, st, pk in g.ro_query("UNWIND $keys AS k MATCH (s:Story)-[:ABOUT]->(p:Page {key: k}) RETURN s.id, s.headline, s.status, p.key",
                                            params={"keys": keys}).result_set:
            nid = "s:" + sid
            if nid not in seen:
                seen.add(nid)
                nodes.append({"id": nid, "kind": "story", "title": head, "status": st})
            links.append({"source": nid, "target": pk, "kind": "about"})
    return {"since": since, "nodes": nodes, "links": links}


@app.get("/api/stories")
def stories(limit: int = 30):
    rows = graph().ro_query("""MATCH (s:Story) OPTIONAL MATCH (s)-[:ABOUT]->(p:Page)
                               WITH s, collect({lang: p.lang, title: p.title, url: p.url}) AS pages
                               OPTIONAL MATCH (b:Brief)-[:FOR]->(s)
                               WITH s, pages, count(b) AS briefs
                               OPTIONAL MATCH (t:Task)-[:TARGETS]->(s)
                               WITH s, pages, briefs, collect(t.status) AS tasks
                               RETURN s ORDER BY s.updated DESC LIMIT $n""", params={"n": limit}).result_set
    # second query for the aggregates keeps the shape simple
    out = []
    for (s,) in rows:
        p = s.properties
        pages = graph().ro_query("MATCH (s:Story {id:$id})-[:ABOUT]->(p:Page) RETURN p.lang, p.title, p.url, p.edits", params={"id": p["id"]}).result_set
        tasks = graph().ro_query("MATCH (t:Task)-[:TARGETS]->(s:Story {id:$id}) RETURN t.status, t.claimed_by ORDER BY t.created", params={"id": p["id"]}).result_set
        nb = graph().ro_query("MATCH (b:Brief)-[:FOR]->(s:Story {id:$id}) RETURN count(b)", params={"id": p["id"]}).result_set[0][0]
        out.append({**p, "pages": [{"lang": a, "title": b, "url": c, "edits": d} for a, b, c, d in pages],
                    "tasks": [{"status": a, "by": b} for a, b in tasks], "briefs": nb})
    return out


@app.get("/api/story/{sid}")
def story(sid: str):
    g = graph()
    s = g.ro_query("MATCH (s:Story {id: $id}) RETURN s", params={"id": sid}).result_set
    if not s:
        return {"error": "not found"}
    briefs = []
    for (b,) in g.ro_query("MATCH (b:Brief)-[:FOR]->(s:Story {id: $id}) RETURN b ORDER BY b.ts DESC", params={"id": sid}).result_set:
        cites = g.ro_query("""MATCH (b:Brief {id: $bid})-[:CITES]->(e:Edit)-[:ON]->(p:Page), (e)<-[:MADE]-(u:Editor)
                              RETURN e.rev, p.lang, p.title, u.name, e.ts, e.delta, e.comment, e.diff_url ORDER BY e.ts""",
                           params={"bid": b.properties["id"]}).result_set
        briefs.append({**b.properties, "cites": [{"rev": r, "lang": l, "title": t, "editor": u, "ts": ts, "delta": d, "comment": c, "diff_url": du}
                                                 for r, l, t, u, ts, d, c, du in cites]})
    pages = g.ro_query("MATCH (s:Story {id:$id})-[:ABOUT]->(p:Page) RETURN p.lang, p.title, p.url, p.edits", params={"id": sid}).result_set
    tasks = g.ro_query("MATCH (t:Task)-[:TARGETS]->(s:Story {id:$id}) RETURN t ORDER BY t.created", params={"id": sid}).result_set
    return {**s[0][0].properties, "briefs": briefs, "pages": [{"lang": a, "title": b, "url": c, "edits": d} for a, b, c, d in pages],
            "tasks": [t.properties for (t,) in tasks]}


class Ask(BaseModel):
    question: str


@app.post("/api/ask")
def ask(body: Ask):
    """Desk agent, streamed as SSE: agent_tool / agent_text / done events."""
    from . import agents
    q: queue.Queue = queue.Queue()

    def run():
        try:
            if not AGENTS_ON:
                q.put({"type": "agent_text", "agent": "desk", "delta": "Agents are offline (no ANTHROPIC_API_KEY)."})
            else:
                agents.desk(body.question, q.put)
        except Exception as e:
            q.put({"type": "agent_text", "agent": "desk", "delta": f"\n\n(error: {e})"})
        q.put({"type": "done"})
    threading.Thread(target=run, daemon=True).start()

    def gen():
        while True:
            ev = q.get()
            yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
            if ev.get("type") == "done":
                break
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
