"""Tremor detection. Everything here is Cypher + FalkorDB graph algorithms; no LLM.

A *tremor* is a page where unusually many distinct humans converge in a short window.
A *story candidate* is a connected component of tremoring pages, where two pages are
connected if the same editors touched both in the window, or they are the same Wikidata
item in different languages. The component's *epicenter* is its PageRank leader.
"""
from __future__ import annotations

import math
import time

from .config import WINDOW_SECONDS
from .store import graph

HOT_PAGES = """
MATCH (u:Editor)-[:MADE]->(e:Edit)-[:ON]->(p:Page)
WHERE e.ts > $since
WITH p, count(e) AS edits, count(DISTINCT u) AS editors,
     sum(CASE WHEN e.revert THEN 1 ELSE 0 END) AS reverts,
     sum(abs(e.delta)) AS churn,
     sum(CASE WHEN u.anon THEN 1 ELSE 0 END) AS anon_edits,
     max(e.ts) AS last_ts
WHERE editors >= $min_editors
RETURN p.key, p.wiki, p.lang, p.title, p.url, edits, editors, reverts, churn, anon_edits, last_ts, p.edits, p.first_ts
"""

# Build the projection: mark hot pages and connect them by shared editors within the window.
MARK_HOT = "UNWIND $keys AS k MATCH (p:Page {key: k}) SET p:Hot"
CLEAR_HOT = "MATCH (p:Hot) REMOVE p:Hot"
CLEAR_CO = "MATCH ()-[r:CO_EDITED]->() DELETE r"
# Hub editors (recent-changes patrollers, vandal fighters) touch dozens of unrelated hot pages; they must not glue
# stories together. Only editors with a small footprint in the window (2..$max_deg hot pages) create CO_EDITED edges.
CO_EDITED = """
MATCH (u:Editor)-[:MADE]->(e:Edit)-[:ON]->(h:Hot) WHERE e.ts > $since
WITH u, count(DISTINCT h) AS deg WHERE deg >= 2 AND deg <= $max_deg
MATCH (a:Hot)<-[:ON]-(ea:Edit)<-[:MADE]-(u)-[:MADE]->(eb:Edit)-[:ON]->(b:Hot)
WHERE ea.ts > $since AND eb.ts > $since AND id(a) < id(b)
WITH a, b, count(DISTINCT u) AS shared
CREATE (a)-[:CO_EDITED {shared: shared}]->(b)
"""
COMPONENTS = """
CALL algo.WCC({nodeLabels: ['Hot'], relationshipTypes: ['CO_EDITED', 'SAME_AS']})
YIELD node, componentId
RETURN node.key, componentId
"""
EPICENTER = """
CALL algo.pageRank('Hot', 'CO_EDITED') YIELD node, score
RETURN node.key, score
"""
SHARED_EDITORS = """
MATCH (a:Hot)-[r:CO_EDITED]-(b:Hot)
RETURN a.key, b.key, r.shared
"""


def tremor_score(row: dict) -> float:
    # editors dominate; edits and reverts amplify; brand-new pages get a bump; long-quiet pages that wake up count more
    base = row["editors"] ** 1.6 * math.log1p(row["edits"])
    base *= 1 + 0.5 * row["reverts"]
    if row["new_page_in_window"]:
        base *= 1.3
    return round(base, 2)


def detect(window: int = WINDOW_SECONDS, min_editors: int = 3, limit: int = 12) -> dict:
    """Return {'pages': [...], 'components': [...], 'window': window, 'since': since}.
    Threshold is adaptive: if fewer than 2 pages have 3+ distinct editors, fall back to 2 (quiet hours)."""
    g = graph()
    since = int(time.time()) - window
    rows = []
    found = g.ro_query(HOT_PAGES, params={"since": since, "min_editors": min_editors}).result_set
    if len(found) < 2 and min_editors > 2:
        min_editors = 2
        found = g.ro_query(HOT_PAGES, params={"since": since, "min_editors": min_editors}).result_set
    for r in found:
        key, wiki, lang, title, url, edits, editors, reverts, churn, anon, last_ts, total_edits, first_ts = r
        row = {"key": key, "wiki": wiki, "lang": lang, "title": title, "url": url, "edits": edits,
               "editors": editors, "reverts": reverts, "churn": churn, "anon_edits": anon, "last_ts": last_ts,
               "total_edits": total_edits, "new_page_in_window": first_ts is not None and first_ts > since}
        row["score"] = tremor_score(row)
        rows.append(row)
    rows.sort(key=lambda r: -r["score"])
    by_key = {r["key"]: r for r in rows}
    components = []
    if rows:
        # projection + algorithms
        g.query(CLEAR_HOT)
        g.query(CLEAR_CO)
        g.query(MARK_HOT, params={"keys": list(by_key)})
        g.query(CO_EDITED, params={"since": since, "max_deg": 4})
        comp_of = {k: c for k, c in g.ro_query(COMPONENTS).result_set}
        rank = {k: s for k, s in g.ro_query(EPICENTER).result_set}
        shared = [(a, b, n) for a, b, n in g.ro_query(SHARED_EDITORS).result_set]
        g.query(CLEAR_CO)
        g.query(CLEAR_HOT)
        groups: dict[int, list[dict]] = {}
        for k, c in comp_of.items():
            groups.setdefault(c, []).append(by_key[k])
        for cid, pages in groups.items():
            pages.sort(key=lambda r: -(rank.get(r["key"], 0) * 10 + r["score"]))
            epicenter = max(pages, key=lambda r: (rank.get(r["key"], 0), r["score"]))
            keys = {p["key"] for p in pages}
            links = [{"a": a, "b": b, "shared": n} for a, b, n in shared if a in keys and b in keys]
            components.append({
                "id": f"c{cid}",
                "score": round(sum(p["score"] for p in pages) * (1 + 0.25 * (len(pages) - 1)), 2),
                "epicenter": epicenter["key"],
                "langs": sorted({p["lang"] for p in pages}),
                "editors": sum(p["editors"] for p in pages),
                "edits": sum(p["edits"] for p in pages),
                "reverts": sum(p["reverts"] for p in pages),
                "pages": pages,
                "links": links,
            })
        components.sort(key=lambda c: -c["score"])
    return {"window": window, "since": since, "min_editors": min_editors, "pages": rows[:60], "components": components[:limit]}


# ---- cross-language linking (Wikidata item ids via the public MediaWiki API) ----------------
import requests
from .config import USER_AGENT

_qid_cache: dict[str, str | None] = {}


def fetch_qids(pages: list[dict]) -> dict[str, str]:
    """Look up Wikidata QIDs for (wiki, title) pairs; batched per wiki. Public API, no auth."""
    out = {}
    by_lang: dict[str, list[dict]] = {}
    for p in pages:
        if p["key"] in _qid_cache:
            if _qid_cache[p["key"]]:
                out[p["key"]] = _qid_cache[p["key"]]
            continue
        by_lang.setdefault(p["lang"], []).append(p)
    for lang, ps in by_lang.items():
        for i in range(0, len(ps), 50):
            chunk = ps[i:i + 50]
            try:
                r = requests.get(f"https://{lang}.wikipedia.org/w/api.php", params={
                    "action": "query", "prop": "pageprops", "ppprop": "wikibase_item", "format": "json",
                    "titles": "|".join(p["title"] for p in chunk), "redirects": 1,
                }, headers={"User-Agent": USER_AGENT}, timeout=10).json()
            except Exception:
                continue
            title_to_qid = {}
            for pg in r.get("query", {}).get("pages", {}).values():
                qid = pg.get("pageprops", {}).get("wikibase_item")
                title_to_qid[pg.get("title")] = qid
            redirects = {x["from"]: x["to"] for x in r.get("query", {}).get("redirects", [])}
            for p in chunk:
                t = redirects.get(p["title"], p["title"])
                qid = title_to_qid.get(t)
                _qid_cache[p["key"]] = qid
                if qid:
                    out[p["key"]] = qid
    return out


def link_same_as(pages: list[dict]) -> int:
    """Set Page.qid and create SAME_AS edges between pages sharing a Wikidata item."""
    qids = fetch_qids(pages)
    if not qids:
        return 0
    g = graph()
    g.query("UNWIND $rows AS r MATCH (p:Page {key: r.key}) SET p.qid = r.qid",
            params={"rows": [{"key": k, "qid": q} for k, q in qids.items()]})
    res = g.query("""UNWIND $qids AS q
                     MATCH (a:Page {qid: q}), (b:Page {qid: q}) WHERE id(a) < id(b)
                     MERGE (a)-[:SAME_AS]->(b)""", params={"qids": sorted(set(qids.values()))})
    return res.relationships_created


if __name__ == "__main__":
    import json
    d = detect()
    link_same_as(d["pages"])
    for c in d["components"][:8]:
        print(c["score"], c["langs"], [(p["title"], p["editors"], p["edits"]) for p in c["pages"]][:5])
