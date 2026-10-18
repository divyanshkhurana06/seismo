"""The agent crew. Coordination and memory live in the graph, not in Python state.

  Scout   (fast model)  looks at detector output + the Story memory, decides what is new,
                        what is an update, what is noise; files (:Task) nodes for the Analyst.
  Analyst (strong model) claims a Task, walks the graph, reads real diffs, files a (:Brief)
                        with (:CITES) edges to the exact (:Edit) nodes that are its evidence.
  Desk    (strong model) answers people's questions from the graph, citing stories/revisions.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Callable, Iterator

import anthropic
from pydantic import BaseModel

from . import signals, wiki
from .config import MODEL_ANALYST, MODEL_SCOUT
from .store import graph

client = anthropic.Anthropic()
Emit = Callable[[dict], None]


def _rows(res) -> list:
    """Serialize a FalkorDB result set into plain JSON-able values."""
    def conv(v):
        if hasattr(v, "properties") and hasattr(v, "labels"):
            return {"_labels": v.labels, **v.properties}
        if hasattr(v, "properties") and hasattr(v, "relation"):
            return {"_type": v.relation, **v.properties}
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v
    return [[conv(v) for v in row] for row in res.result_set]


# ------------------------------------------------------------------ graph-facing tools
READ_ONLY_BANNED = ("create ", "merge ", "delete ", "set ", "remove ", "drop ", "detach ")


def tool_graph_query(cypher: str) -> str:
    low = " " + cypher.lower()
    if any(b in low for b in READ_ONLY_BANNED):
        return "error: read-only. Use MATCH/RETURN/CALL algo.* only."
    try:
        res = graph().ro_query(cypher, timeout=8000)
    except Exception as e:
        return f"error: {e}"
    rows = _rows(res)
    return json.dumps({"columns": res.header and [h[1] for h in res.header], "rows": rows[:60], "truncated": len(rows) > 60},
                      ensure_ascii=False, default=str)[:12000]


def tool_story_dossier(story_id: str, recent: int = 40) -> str:
    g = graph()
    s = g.ro_query("MATCH (s:Story {id: $id}) RETURN s", params={"id": story_id}).result_set
    if not s:
        return "error: no such story"
    story = s[0][0].properties
    pages = _rows(g.ro_query("""MATCH (s:Story {id: $id})-[:ABOUT]->(p:Page)
                                RETURN p.key, p.lang, p.title, p.url, p.edits, p.qid""", params={"id": story_id}))
    edits = _rows(g.ro_query("""MATCH (s:Story {id: $id})-[:ABOUT]->(p:Page)<-[:ON]-(e:Edit)<-[:MADE]-(u:Editor)
                                RETURN e.rev, p.lang, p.title, u.name, u.anon, e.ts, e.delta, e.revert, e.comment
                                ORDER BY e.ts DESC LIMIT $n""", params={"id": story_id, "n": recent}))
    briefs = _rows(g.ro_query("""MATCH (b:Brief)-[:FOR]->(s:Story {id: $id})
                                 RETURN b.ts, b.headline, b.status, b.confidence, b.summary ORDER BY b.ts DESC LIMIT 5""",
                              params={"id": story_id}))
    editors = _rows(g.ro_query("""MATCH (s:Story {id: $id})-[:ABOUT]->(p:Page)<-[:TOUCHED]-(u:Editor)
                                  WITH u, count(DISTINCT p) AS pages, sum(u.edits) AS e
                                  RETURN u.name, u.anon, pages, u.edits ORDER BY pages DESC, u.edits DESC LIMIT 15""",
                               params={"id": story_id}))
    return json.dumps({"story": story, "pages": pages,
                       "recent_edits (rev, lang, title, editor, anon, ts, delta, revert, comment)": edits,
                       "prior_briefs": briefs, "editors_across_pages (name, anon, pages_touched, total_edits)": editors},
                      ensure_ascii=False, default=str)[:14000]


def tool_read_diff(lang: str, rev: int) -> str:
    return wiki.read_diff(lang, int(rev))


def tool_page_summary(lang: str, title: str) -> str:
    return wiki.page_summary(lang, title)


def tool_current_stories(limit: int = 12) -> str:
    rows = _rows(graph().ro_query("""MATCH (s:Story) OPTIONAL MATCH (s)-[:ABOUT]->(p:Page)
                                     WITH s, collect(p.lang + ':' + p.title)[0..6] AS pages
                                     RETURN s.id, s.headline, s.status, s.confidence, s.updated, s.summary, pages
                                     ORDER BY s.updated DESC LIMIT $n""", params={"n": limit}))
    return json.dumps(rows, ensure_ascii=False, default=str)[:12000]


def tool_tremors_now() -> str:
    d = signals.detect()
    out = [{"component": c["id"], "score": c["score"], "langs": c["langs"],
            "pages": [{"lang": p["lang"], "title": p["title"], "editors": p["editors"], "edits": p["edits"], "reverts": p["reverts"]}
                      for p in c["pages"][:6]]} for c in d["components"][:8]]
    return json.dumps(out, ensure_ascii=False)


GRAPH_SCHEMA_DOC = """Graph schema (FalkorDB, Cypher):
(:Page {key, wiki, lang, title, url, qid, edits, first_ts, last_ts})
(:Editor {name, anon, edits, last_ts})   -- anon editors are hashed ids, never IPs
(:Edit {rev, ts, delta, comment, revert, new_page, minor, diff_url})
(:Editor)-[:MADE]->(:Edit)-[:ON]->(:Page)
(:Editor)-[:TOUCHED {n, last_ts}]->(:Page)
(:Page)-[:SAME_AS]->(:Page)              -- same Wikidata item, other language
(:Story {id, headline, summary, status, confidence, created, updated, score})-[:ABOUT]->(:Page)
(:Brief {id, ts, headline, summary, status, confidence, agent})-[:FOR]->(:Story), (:Brief)-[:CITES]->(:Edit)
(:Task {id, kind, status, priority, created, claimed_by, note})-[:TARGETS]->(:Story)
Timestamps are unix seconds. Fulltext: CALL db.idx.fulltext.queryNodes('Page', 'text') YIELD node, score; same for 'Story'.
Algorithms: CALL algo.pageRank(label, rel), CALL algo.WCC({nodeLabels, relationshipTypes}),
CALL algo.labelPropagation({...}), CALL algo.betweenness({...}), CALL algo.SPpaths({sourceNode, targetNode, relTypes}).
"""

TOOLS_ANALYST = [
    {"name": "graph_query", "description": "Run a read-only Cypher query against the live edit graph. " + GRAPH_SCHEMA_DOC,
     "input_schema": {"type": "object", "properties": {"cypher": {"type": "string"}}, "required": ["cypher"], "additionalProperties": False}, "strict": True},
    {"name": "story_dossier", "description": "Everything the graph knows about a story: pages, recent edits with comments, editors spanning pages, prior briefs (memory).",
     "input_schema": {"type": "object", "properties": {"story_id": {"type": "string"}}, "required": ["story_id"], "additionalProperties": False}, "strict": True},
    {"name": "read_diff", "description": "Fetch the actual text added/removed by one revision from Wikipedia. This is your primary evidence; read several before concluding.",
     "input_schema": {"type": "object", "properties": {"lang": {"type": "string"}, "rev": {"type": "integer"}}, "required": ["lang", "rev"], "additionalProperties": False}, "strict": True},
    {"name": "page_summary", "description": "Short intro of a Wikipedia article (for context on what the page is).",
     "input_schema": {"type": "object", "properties": {"lang": {"type": "string"}, "title": {"type": "string"}}, "required": ["lang", "title"], "additionalProperties": False}, "strict": True},
    {"name": "file_brief", "description": "File your finished brief. Call exactly once, at the end.",
     "input_schema": {"type": "object", "properties": {
         "headline": {"type": "string", "description": "<= 90 chars, newsroom style, English"},
         "summary": {"type": "string", "description": "3-6 sentences: what is happening, what the edits say, how you know, what is uncertain."},
         "status": {"type": "string", "enum": ["breaking", "developing", "edit_war", "vandalism", "routine", "unclear"]},
         "confidence": {"type": "number", "description": "0..1"},
         "cited_revs": {"type": "array", "items": {"type": "integer"}, "description": "revision ids whose diffs you actually read and relied on"},
         "tags": {"type": "array", "items": {"type": "string"}}},
         "required": ["headline", "summary", "status", "confidence", "cited_revs", "tags"], "additionalProperties": False}, "strict": True},
]
TOOLS_DESK = [TOOLS_ANALYST[0], TOOLS_ANALYST[1], TOOLS_ANALYST[2], TOOLS_ANALYST[3],
              {"name": "current_stories", "description": "Stories the crew is tracking right now, newest first.",
               "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}, "strict": True},
              {"name": "tremors_now", "description": "Run the tremor detector (editor convergence + WCC + PageRank) on the last 20 minutes.",
               "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}, "strict": True}]

ANALYST_SYSTEM = """You are the Analyst on Seismo, a newsroom whose only sense organ is the live graph of Wikipedia edits.
You never see headlines. You infer what is happening in the world from who is editing what, together, right now,
and from the text of the edits themselves. Work like a wire-service reporter: evidence first, then a careful claim.

Method: pull the story dossier; look at which pages, how many distinct humans, reverts, languages; read the diffs of the
most consequential revisions (large deltas, non-minor, several editors); use graph_query when you need more (e.g. who else
these editors touched, whether SAME_AS pages in other languages moved). Distinguish real-world events from vandalism
sprees and edit wars (reverts ping-ponging between two editors). Be explicit about uncertainty. Then call file_brief once.
Cite only revisions you actually read. Keep it tight."""

DESK_SYSTEM = """You are the Desk at Seismo. People ask you what is going on in the world and you answer from the live
Wikipedia edit graph in FalkorDB: the stories the crew has filed, the tremors happening right now, and the raw edits.
Use the tools; do not guess. Cite evidence inline as [story:<id>] and [rev:<lang>:<rev>] so the UI can link them.
If the graph is quiet, say so. Answer in a few short paragraphs or bullets, newsroom tone, no fluff."""


def _run_tools(name: str, inp: dict) -> str:
    if name == "graph_query":
        return tool_graph_query(inp["cypher"])
    if name == "story_dossier":
        return tool_story_dossier(inp["story_id"])
    if name == "read_diff":
        return tool_read_diff(inp["lang"], inp["rev"])
    if name == "page_summary":
        return tool_page_summary(inp["lang"], inp["title"])
    if name == "current_stories":
        return tool_current_stories()
    if name == "tremors_now":
        return tool_tremors_now()
    return f"error: unknown tool {name}"


def agent_loop(agent: str, model: str, system: str, tools: list, user: str, emit: Emit,
               effort: str = "medium", max_turns: int = 14, on_final_tool: Callable[[dict], str] | None = None) -> dict:
    """Manual tool loop that narrates every step through emit(). Returns {'text':..., 'final_tool': {...}|None, 'usage':...}."""
    messages = [{"role": "user", "content": user}]
    final_tool = None
    text_out = []
    usage = {"in": 0, "out": 0}
    for turn in range(max_turns):
        with client.messages.stream(model=model, max_tokens=8000, system=system, tools=tools, messages=messages,
                                    output_config={"effort": effort}) as stream:
            for ev in stream:
                if ev.type == "content_block_delta" and getattr(ev.delta, "type", "") == "text_delta":
                    emit({"type": "agent_text", "agent": agent, "delta": ev.delta.text})
            resp = stream.get_final_message()
        usage["in"] += resp.usage.input_tokens
        usage["out"] += resp.usage.output_tokens
        if resp.stop_reason == "refusal":
            emit({"type": "agent_log", "agent": agent, "msg": "request declined by safety system"})
            break
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                text_out.append(b.text)
        messages.append({"role": "assistant", "content": resp.content})
        if not tool_uses:
            break
        results = []
        for tu in tool_uses:
            inp = tu.input if isinstance(tu.input, dict) else json.loads(tu.input)
            emit({"type": "agent_tool", "agent": agent, "tool": tu.name, "input": inp})
            if on_final_tool and tu.name == "file_brief":
                out = on_final_tool(inp)
                final_tool = inp
            else:
                t0 = time.time()
                out = _run_tools(tu.name, inp)
                emit({"type": "agent_tool_result", "agent": agent, "tool": tu.name, "ms": int((time.time() - t0) * 1000),
                      "preview": out[:300]})
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
        messages.append({"role": "user", "content": results})
        if final_tool:
            break
    return {"text": "\n".join(text_out), "final_tool": final_tool, "usage": usage}


# ------------------------------------------------------------------ Scout
class Decision(BaseModel):
    component: str
    action: str            # "new" | "update" | "ignore"
    story_id: str | None = None
    working_title: str
    priority: int          # 1 (meh) .. 5 (drop everything)
    reason: str


class ScoutOutput(BaseModel):
    decisions: list[Decision]


SCOUT_SYSTEM = """You are the Scout on Seismo, a newsroom that senses world events through Wikipedia's live edit graph.
You receive tremor components (clusters of pages that unrelated humans are suddenly editing together) and the stories we
already track (memory). For each component decide: 'new' story, 'update' to an existing story (same pages or same topic),
or 'ignore' (routine maintenance, a single vandal, bot-like cleanup, trivial churn). Prefer updates over duplicates.
Priority: multi-page, multi-language, many distinct editors, large deltas, reverts => high. Working titles are neutral
descriptions of the cluster, not claims about what happened (the Analyst verifies). Return every component."""


def scout(components: list[dict], emit: Emit) -> list[dict]:
    """Decide what to investigate; create Story + Task nodes. Returns decisions."""
    if not components:
        return []
    g = graph()
    # Memory check before spending tokens: skip clusters already under investigation or briefed in the last 10 min.
    fresh = []
    for c in components:
        busy = g.ro_query("""UNWIND $keys AS k MATCH (p:Page {key: k})<-[:ABOUT]-(s:Story)
                             OPTIONAL MATCH (t:Task)-[:TARGETS]->(s) WHERE t.status IN ['open', 'claimed']
                             RETURN count(t) > 0 OR max(s.updated) > $recent""",
                          params={"keys": [p["key"] for p in c["pages"]], "recent": int(time.time()) - 600}).result_set
        if busy and busy[0][0]:
            continue
        fresh.append(c)
    if not fresh:
        emit({"type": "agent_log", "agent": "scout", "msg": f"{len(components)} tremor components, all already covered (graph memory); nothing to file"})
        return []
    components = fresh
    stories = _rows(g.ro_query("""MATCH (s:Story) OPTIONAL MATCH (s)-[:ABOUT]->(p:Page)
                                  WITH s, collect(p.key) AS keys
                                  RETURN s.id, s.headline, s.status, s.updated, keys ORDER BY s.updated DESC LIMIT 25"""))
    payload = {"now": int(time.time()),
               "components": [{"id": c["id"], "score": c["score"], "langs": c["langs"], "editors": c["editors"], "edits": c["edits"],
                               "reverts": c["reverts"], "pages": [{"key": p["key"], "title": p["title"], "lang": p["lang"],
                                                                   "editors": p["editors"], "edits": p["edits"], "reverts": p["reverts"],
                                                                   "churn": p["churn"], "anon_edits": p["anon_edits"]} for p in c["pages"][:8]]}
                              for c in components],
               "tracked_stories (id, headline, status, updated, page_keys)": stories}
    emit({"type": "agent_log", "agent": "scout", "msg": f"triaging {len(components)} tremor components against {len(stories)} tracked stories"})
    resp = client.messages.parse(model=MODEL_SCOUT, max_tokens=4000, system=SCOUT_SYSTEM, output_format=ScoutOutput,
                                 output_config={"effort": "low"},
                                 messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
    decisions = resp.parsed_output.decisions if resp.parsed_output else []
    by_id = {c["id"]: c for c in components}
    out = []
    for d in decisions:
        c = by_id.get(d.component)
        if not c or d.action == "ignore":
            emit({"type": "agent_log", "agent": "scout", "msg": f"ignore {d.component}: {d.reason}"})
            out.append(d.model_dump())
            continue
        if d.action == "update" and d.story_id:
            sid = d.story_id
            g.query("MATCH (s:Story {id: $id}) SET s.updated = $now, s.score = $score", params={"id": sid, "now": int(time.time()), "score": c["score"]})
        else:
            sid = "s_" + uuid.uuid4().hex[:8]
            g.query("""CREATE (:Story {id: $id, headline: $title, summary: '', status: 'candidate', confidence: 0.0,
                                       created: $now, updated: $now, score: $score})""",
                    params={"id": sid, "title": d.working_title, "now": int(time.time()), "score": c["score"]})
        g.query("UNWIND $keys AS k MATCH (s:Story {id: $id}), (p:Page {key: k}) MERGE (s)-[:ABOUT]->(p)",
                params={"id": sid, "keys": [p["key"] for p in c["pages"]]})
        # hand-off: a Task node is the message to the Analyst
        tid = "t_" + uuid.uuid4().hex[:8]
        g.query("""MATCH (s:Story {id: $sid})
                   CREATE (t:Task {id: $tid, kind: 'investigate', status: 'open', priority: $prio, created: $now,
                                   note: $note, claimed_by: ''})-[:TARGETS]->(s)""",
                params={"sid": sid, "tid": tid, "prio": d.priority, "now": int(time.time()), "note": d.reason})
        emit({"type": "agent_log", "agent": "scout", "msg": f"{d.action} story {sid} (p{d.priority}): {d.working_title} — {d.reason}"})
        emit({"type": "task", "task": tid, "story": sid, "status": "open", "priority": d.priority})
        out.append({**d.model_dump(), "story_id": sid, "task_id": tid})
    return out


# ------------------------------------------------------------------ Analyst
def claim_task(worker: str) -> dict | None:
    g = graph()
    rows = g.query("""MATCH (t:Task {status: 'open'})-[:TARGETS]->(s:Story)
                      WITH t, s ORDER BY t.priority DESC, t.created ASC LIMIT 1
                      SET t.status = 'claimed', t.claimed_by = $w, t.claimed_at = $now
                      RETURN t.id, s.id, t.priority, t.note""", params={"w": worker, "now": int(time.time())}).result_set
    if not rows:
        return None
    tid, sid, prio, note = rows[0]
    return {"task": tid, "story": sid, "priority": prio, "note": note}


def analyst(task: dict, emit: Emit, worker: str = "analyst-1") -> dict | None:
    g = graph()
    sid, tid = task["story"], task["task"]
    emit({"type": "task", "task": tid, "story": sid, "status": "claimed", "by": worker})

    def file_brief(inp: dict) -> str:
        bid = "b_" + uuid.uuid4().hex[:8]
        now = int(time.time())
        g.query("""MATCH (s:Story {id: $sid})
                   CREATE (b:Brief {id: $bid, ts: $now, headline: $h, summary: $sum, status: $st, confidence: $conf,
                                    agent: $agent, tags: $tags})-[:FOR]->(s)
                   SET s.headline = $h, s.summary = $sum, s.status = $st, s.confidence = $conf, s.updated = $now""",
                params={"sid": sid, "bid": bid, "now": now, "h": inp["headline"][:140], "sum": inp["summary"],
                        "st": inp["status"], "conf": float(inp["confidence"]), "agent": worker, "tags": inp.get("tags", [])})
        cited = g.query("""UNWIND $revs AS r MATCH (b:Brief {id: $bid}), (e:Edit {rev: r}) MERGE (b)-[:CITES]->(e)""",
                        params={"bid": bid, "revs": [int(r) for r in inp.get("cited_revs", [])]}).relationships_created
        return f"filed {bid}; {cited} citations linked"

    prompt = (f"Investigate story {sid} (task {tid}, priority {task['priority']}). Scout's note: {task['note']}\n"
              f"Start with story_dossier('{sid}').")
    try:
        result = agent_loop("analyst", MODEL_ANALYST, ANALYST_SYSTEM, TOOLS_ANALYST, prompt, emit,
                            effort="medium", on_final_tool=file_brief)
    except Exception as e:
        g.query("MATCH (t:Task {id: $id}) SET t.status = 'failed', t.note = $e", params={"id": tid, "e": str(e)[:300]})
        emit({"type": "agent_log", "agent": "analyst", "msg": f"failed: {e}"})
        return None
    status = "done" if result["final_tool"] else "no_brief"
    g.query("MATCH (t:Task {id: $id}) SET t.status = $st, t.done_at = $now, t.tokens_in = $i, t.tokens_out = $o",
            params={"id": tid, "st": status, "now": int(time.time()), "i": result["usage"]["in"], "o": result["usage"]["out"]})
    emit({"type": "task", "task": tid, "story": sid, "status": status})
    if result["final_tool"]:
        emit({"type": "brief", "story": sid, **result["final_tool"]})
    return result


# ------------------------------------------------------------------ Desk
def desk(question: str, emit: Emit) -> str:
    result = agent_loop("desk", MODEL_ANALYST, DESK_SYSTEM, TOOLS_DESK, question, emit, effort="low", max_turns=10)
    return result["text"]
