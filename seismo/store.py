"""Graph writer: turns the edit stream into the FalkorDB context graph.

Model
  (:Page   {key, wiki, lang, title, url, qid?, edits, last_ts})
  (:Editor {name, anon, edits})
  (:Edit   {rev, ts, delta, comment, revert, new_page, minor, diff_url})
  (:Editor)-[:MADE]->(:Edit)-[:ON]->(:Page)
  (:Editor)-[:TOUCHED {n, last_ts}]->(:Page)         incremental projection used by the detectors
  (:Page)-[:SAME_AS]->(:Page)                         same Wikidata item across languages
memory / coordination (written by agents, see agents.py)
  (:Story)-[:ABOUT]->(:Page)   (:Brief)-[:FOR]->(:Story)   (:Brief)-[:CITES]->(:Edit)
  (:Task)-[:TARGETS]->(:Story)
"""
from __future__ import annotations

import threading
import time
from functools import lru_cache

from falkordb import FalkorDB, Graph

from .config import FALKORDB_URL, GRAPH_NAME, RETAIN_SECONDS

SCHEMA = [
    "CREATE INDEX FOR (p:Page) ON (p.key)",
    "CREATE INDEX FOR (p:Page) ON (p.qid)",
    "CREATE INDEX FOR (p:Page) ON (p.last_ts)",
    "CREATE INDEX FOR (e:Editor) ON (e.name)",
    "CREATE INDEX FOR (e:Edit) ON (e.rev)",
    "CREATE INDEX FOR (e:Edit) ON (e.ts)",
    "CREATE INDEX FOR (s:Story) ON (s.id)",
    "CREATE INDEX FOR (t:Task) ON (t.id)",
    "CREATE INDEX FOR (b:Brief) ON (b.id)",
    "CALL db.idx.fulltext.createNodeIndex('Page', 'title')",
    "CALL db.idx.fulltext.createNodeIndex('Story', 'headline', 'summary')",
]


@lru_cache(maxsize=1)
def db() -> FalkorDB:
    return FalkorDB.from_url(FALKORDB_URL)


def graph() -> Graph:
    return db().select_graph(GRAPH_NAME)


def ensure_schema(reset: bool = False) -> None:
    g = graph()
    if reset:
        try:
            g.delete()
        except Exception:
            pass
        g = graph()
    for q in SCHEMA:
        try:
            g.query(q)
        except Exception:
            pass  # index already exists


UPSERT = """
UNWIND $rows AS r
MERGE (p:Page {key: r.wiki + '|' + r.title})
  ON CREATE SET p.wiki = r.wiki, p.lang = r.lang, p.title = r.title, p.url = r.url, p.first_ts = r.ts, p.edits = 0
SET p.last_ts = r.ts, p.edits = p.edits + 1
MERGE (u:Editor {name: r.editor})
  ON CREATE SET u.anon = r.anon, u.edits = 0
SET u.edits = u.edits + 1, u.last_ts = r.ts
CREATE (u)-[:MADE]->(e:Edit {rev: r.rev, ts: r.ts, delta: r.delta, comment: r.comment, revert: r.revert,
                             new_page: r.new_page, minor: r.minor, diff_url: r.diff_url})-[:ON]->(p)
MERGE (u)-[t:TOUCHED]->(p)
  ON CREATE SET t.n = 1, t.last_ts = r.ts
  ON MATCH SET t.n = t.n + 1, t.last_ts = r.ts
"""

PRUNE = """
MATCH (e:Edit) WHERE e.ts < $cutoff
WITH e LIMIT 5000
DETACH DELETE e
"""
PRUNE_ORPHANS = """
MATCH (p:Page) WHERE p.last_ts < $cutoff AND NOT (p)<-[:ABOUT]-() AND NOT (p)<-[:ON]-()
WITH p LIMIT 5000 DETACH DELETE p
"""


class GraphWriter:
    """Buffers events and flushes them in one UNWIND per batch."""

    def __init__(self, flush_every: float = 1.0, max_batch: int = 400):
        self.g = graph()
        self.buf: list[dict] = []
        self.lock = threading.Lock()
        self.flush_every = flush_every
        self.max_batch = max_batch
        self.total = 0
        self.last_prune = time.time()
        self.listeners: list = []  # callables(event) for the UI broadcaster

    def add(self, ev: dict) -> None:
        with self.lock:
            self.buf.append(ev)
            n = len(self.buf)
        if n >= self.max_batch:
            self.flush()

    def flush(self) -> int:
        with self.lock:
            rows, self.buf = self.buf, []
        if not rows:
            return 0
        self.g.query(UPSERT, params={"rows": rows})
        self.total += len(rows)
        for fn in self.listeners:
            try:
                fn(rows)
            except Exception:
                pass
        if time.time() - self.last_prune > 120:
            self.prune()
        return len(rows)

    def prune(self) -> None:
        cutoff = int(time.time()) - RETAIN_SECONDS
        self.g.query(PRUNE, params={"cutoff": cutoff})
        self.g.query(PRUNE_ORPHANS, params={"cutoff": cutoff})
        self.last_prune = time.time()

    def run(self, source) -> None:
        """Consume an event iterator forever, flushing on a timer."""
        last = time.time()
        for ev in source:
            self.add(ev)
            if time.time() - last >= self.flush_every:
                self.flush()
                last = time.time()
