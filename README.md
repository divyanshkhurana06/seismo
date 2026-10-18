# Seismo

**World events, felt through Wikipedia's live edit graph.**

News doesn't get *written* on Wikipedia. It gets *edited*. When something happens in the world, strangers who have never
coordinated converge on the same handful of articles within minutes, in several languages at once. That convergence has a
shape, and the shape appears before any headline does.

Seismo is a newsroom whose only sense organ is that shape. It streams every human edit on every Wikipedia into FalkorDB as a
live graph, detects *tremors* (unrelated editors suddenly clustering on connected pages) with Cypher and FalkorDB's graph
algorithms, and hands them to a crew of Claude agents that read the actual diffs and file cited briefs. The agents never see
a news feed. Everything they know, they know from the graph.

Built solo by **Divyansh Khurana** for Graph Hacks 2026 (WeMakeDevs × FalkorDB), Oct 15–18 2026.

**Tracks entered:** 01 Agents That Act on Connected Data · 02 Agent Memory and Coordination

---

## What you see

- **Live graph** of the pages people are editing *right now*, their editors, cross-language twins, and the stories the crew
  is tracking. Nodes pulse as edits land. Hot pages (3+ distinct humans in 20 min) glow red.
- **Seismograph**: edits per minute across all Wikipedias, reverts in red.
- **Tremors**: components of hot pages linked by shared editors or by being the same Wikidata item in another language,
  ranked by score, with the PageRank *epicenter* of each.
- **Stories**: what the Scout filed and what the Analyst concluded, with confidence and a citation for every revision it
  actually read.
- **Crew**: the agents' tool calls and hand-offs as they happen.
- **Ask the Desk**: "what is happening right now?", "is the thing on the French wiki related to the English one?",
  "who is edit-warring?" Answered from the graph with `[story:…]` and `[rev:…]` citations the UI turns into links.

## Why a graph, and why it has to be FalkorDB

The signal *is* the topology. A page with ten edits by one person is maintenance. Ten edits by eight people who also just
touched three neighbouring pages in two languages is an event. That is a question about paths between editors and pages,
not about any single row, and it has to be answered every 45 seconds over a graph that grows by ~2 edits/second.

- **Editor convergence** is a one-hop aggregation over `(:Editor)-[:MADE]->(:Edit)-[:ON]->(:Page)`.
- **Story candidates** are *connected components* over a projection built on the fly: hot pages get a temporary `:Hot`
  label, `CO_EDITED` edges are created between pages sharing editors in the window, and `algo.WCC` finds the clusters.
- **Epicenters** come from `algo.pageRank` over that same projection.
- **Cross-language cascades** use `SAME_AS` edges between pages that resolve to the same Wikidata item.
- **Agent memory and coordination** are nodes too: `Story`, `Brief`, `Task`. The Scout checks the graph before spending a
  single token ("is this cluster already under investigation?"), the Analyst claims work by mutating a `Task` node, and every
  claim in a brief has a `CITES` edge to the exact `Edit` it rests on. Restart the process and the crew remembers.

Remove FalkorDB and nothing works: the detector, the hand-offs and the memory are all Cypher.

## Graph model

```
(:Page   {key, wiki, lang, title, url, qid, edits, first_ts, last_ts})
(:Editor {name, anon, edits, last_ts})           anon editors are salted hashes; no IPs enter the graph
(:Edit   {rev, ts, delta, comment, revert, new_page, minor, diff_url})

(:Editor)-[:MADE]->(:Edit)-[:ON]->(:Page)
(:Editor)-[:TOUCHED {n, last_ts}]->(:Page)       incremental projection, kept up to date on every write
(:Page)-[:SAME_AS]->(:Page)                      same Wikidata item in another language (public MediaWiki API)

(:Story {id, headline, summary, status, confidence, created, updated, score})-[:ABOUT]->(:Page)
(:Brief {id, ts, headline, summary, status, confidence, agent, tags})-[:FOR]->(:Story)
(:Brief)-[:CITES]->(:Edit)                       evidence, revision by revision
(:Task  {id, kind, status, priority, created, claimed_by, note, tokens_in, tokens_out})-[:TARGETS]->(:Story)
```

Transient, during detection only: `:Hot` label on pages in the window, `(:Hot)-[:CO_EDITED {shared}]->(:Hot)`.

Indexes: `Page.key`, `Page.qid`, `Page.last_ts`, `Editor.name`, `Edit.rev`, `Edit.ts`, `Story.id`, `Task.id`, `Brief.id`,
full-text on `Page.title` and `Story.headline/summary`.

## The Cypher the product depends on

**Ingest** (one `UNWIND` per batch, `seismo/store.py`):

```cypher
UNWIND $rows AS r
MERGE (p:Page {key: r.wiki + '|' + r.title})
  ON CREATE SET p.wiki = r.wiki, p.lang = r.lang, p.title = r.title, p.url = r.url, p.first_ts = r.ts, p.edits = 0
SET p.last_ts = r.ts, p.edits = p.edits + 1
MERGE (u:Editor {name: r.editor}) ON CREATE SET u.anon = r.anon, u.edits = 0
SET u.edits = u.edits + 1, u.last_ts = r.ts
CREATE (u)-[:MADE]->(e:Edit {rev: r.rev, ts: r.ts, delta: r.delta, comment: r.comment, revert: r.revert,
                             new_page: r.new_page, minor: r.minor, diff_url: r.diff_url})-[:ON]->(p)
MERGE (u)-[t:TOUCHED]->(p) ON CREATE SET t.n = 1, t.last_ts = r.ts ON MATCH SET t.n = t.n + 1, t.last_ts = r.ts
```

**Tremor candidates** (`seismo/signals.py`):

```cypher
MATCH (u:Editor)-[:MADE]->(e:Edit)-[:ON]->(p:Page) WHERE e.ts > $since
WITH p, count(e) AS edits, count(DISTINCT u) AS editors,
     sum(CASE WHEN e.revert THEN 1 ELSE 0 END) AS reverts, sum(abs(e.delta)) AS churn,
     sum(CASE WHEN u.anon THEN 1 ELSE 0 END) AS anon_edits, max(e.ts) AS last_ts
WHERE editors >= $min_editors
RETURN p.key, p.wiki, p.lang, p.title, p.url, edits, editors, reverts, churn, anon_edits, last_ts, p.edits, p.first_ts
```

**Projection + algorithms** (hot pages → shared-editor edges → components → epicenter):

```cypher
UNWIND $keys AS k MATCH (p:Page {key: k}) SET p:Hot;

MATCH (a:Hot)<-[:ON]-(ea:Edit)<-[:MADE]-(u:Editor)-[:MADE]->(eb:Edit)-[:ON]->(b:Hot)
WHERE ea.ts > $since AND eb.ts > $since AND id(a) < id(b)
WITH a, b, count(DISTINCT u) AS shared
CREATE (a)-[:CO_EDITED {shared: shared}]->(b);

CALL algo.WCC({nodeLabels: ['Hot'], relationshipTypes: ['CO_EDITED', 'SAME_AS']}) YIELD node, componentId
RETURN node.key, componentId;

CALL algo.pageRank('Hot', 'CO_EDITED') YIELD node, score RETURN node.key, score;

MATCH ()-[r:CO_EDITED]->() DELETE r;  MATCH (p:Hot) REMOVE p:Hot;
```

**Cross-language twins** (after resolving Wikidata ids for hot pages):

```cypher
UNWIND $qids AS q
MATCH (a:Page {qid: q}), (b:Page {qid: q}) WHERE id(a) < id(b)
MERGE (a)-[:SAME_AS]->(b)
```

**Scout memory check** (before any LLM call: is this cluster already covered?):

```cypher
UNWIND $keys AS k MATCH (p:Page {key: k})<-[:ABOUT]-(s:Story)
OPTIONAL MATCH (t:Task)-[:TARGETS]->(s) WHERE t.status IN ['open', 'claimed']
RETURN count(t) > 0 OR max(s.updated) > $recent
```

**Hand-off** (Scout files work; an Analyst claims it atomically):

```cypher
MATCH (s:Story {id: $sid})
CREATE (t:Task {id: $tid, kind: 'investigate', status: 'open', priority: $prio, created: $now, note: $note})-[:TARGETS]->(s);

MATCH (t:Task {status: 'open'})-[:TARGETS]->(s:Story)
WITH t, s ORDER BY t.priority DESC, t.created ASC LIMIT 1
SET t.status = 'claimed', t.claimed_by = $w, t.claimed_at = $now
RETURN t.id, s.id, t.priority, t.note
```

**Evidence** (every brief points at the revisions it read):

```cypher
UNWIND $revs AS r MATCH (b:Brief {id: $bid}), (e:Edit {rev: r}) MERGE (b)-[:CITES]->(e)
```

The Analyst and the Desk also write their own read-only Cypher through the `graph_query` tool (the schema above is in their
tool description); you can watch those queries in the Crew tab.

## Architecture

```
Wikimedia EventStreams (SSE, all wikipedias, ns0, humans only)
        │  seismo/stream.py   normalize · hash anonymous editors · JSONL record/replay
        ▼
   GraphWriter  ──UNWIND batches──▶  FalkorDB  ◀──read/write──┐
   seismo/store.py                     ▲    ▲                 │
                                       │    │                 │
   every 45 s: signals.detect()  ──────┘    │       Scout (claude-sonnet-5-5, structured output)
   Cypher + algo.WCC + algo.pageRank        │         decides new / update / ignore, writes Story + Task
   + SAME_AS via Wikidata                   │       Analyst ×2 (claude-opus-5-5, tool loop)
                                            │         claims Task, story_dossier · graph_query · read_diff · page_summary
   FastAPI + SSE  (seismo/server.py)  ──────┘         files Brief with CITES edges
        │                                           Desk (claude-opus-5-5) answers questions, cites [story:] [rev:]
        ▼
   static/index.html  force-graph canvas · seismograph · stories · crew log · ask box
```

Agents (`seismo/agents.py`) use the Anthropic Python SDK directly: a manual tool loop so every step can be streamed to the
UI, `strict` tool schemas, `output_config.effort` tuned per role, and `messages.parse` with a Pydantic model for the Scout.

## Run it

Prereqs: Docker, Python 3.11+, an Anthropic API key (without a key the stream, graph, detector and UI still run; only the
crew is offline).

```bash
docker run -d --name seismo-falkordb -p 6380:6379 -p 3001:3000 falkordb/falkordb:latest
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/uvicorn seismo.server:app --port 8790
```

Open http://localhost:8790. FalkorDB Browser is on http://localhost:3001 (graph name `seismo`).

Or all at once: `cp .env.example .env`, edit it, then `docker compose up --build` and open http://localhost:8080.

**Replay mode** for demos or quiet hours (ships with ~25 minutes of real public edits, timeline compressed 20×):

```bash
SEISMO_SOURCE=replay:data/sample.jsonl:20 .venv/bin/uvicorn seismo.server:app --port 8790
```

Record your own: `python -m seismo.stream record data/capture.jsonl`.

Useful env vars: `FALKORDB_URL`, `SEISMO_WINDOW` (detection window, s), `SEISMO_RETAIN` (history kept, s),
`SEISMO_DETECT_EVERY`, `SEISMO_ANALYSTS`, `SEISMO_MODEL_ANALYST`, `SEISMO_MODEL_SCOUT`.

## Data and privacy

Only public data: the Wikimedia recent-changes stream and the public MediaWiki/Wikidata APIs. Anonymous editors appear in
the stream as IP addresses; Seismo replaces them with a salted hash before anything is written, so no IP is ever stored.
Edits older than `SEISMO_RETAIN` (default 6 h) are pruned; stories and briefs persist.

## Disclosure

I used an AI coding assistant (Claude) while building this during the hackathon window, as the rules allow. The idea, the
graph model, the detection design and every decision in this repo are mine, and I can walk through all of it.
