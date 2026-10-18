# Demo script (target: 2.5–3 minutes)

Record with the UI full-screen. If the live stream is quiet, run replay mode (`SEISMO_SOURCE=replay:data/sample.jsonl:20`)
so tremors appear within a minute. Keep the Crew tab visible when agents are working; that is the money shot.

## 0:00 — Hook (graph tab)
"News doesn't get written on Wikipedia. It gets edited. When something happens, strangers converge on the same articles
in several languages within minutes. Seismo is a newsroom whose only sense organ is that convergence, and the graph it
watches lives in FalkorDB."

Point at the pulsing nodes: "every pulse is a human edit landing in the graph right now, about two a second across every
Wikipedia."

## 0:30 — The graph does the work (Tremors tab)
"Every 45 seconds the detector runs pure Cypher: which pages have three-plus distinct humans in twenty minutes. Then it
builds a projection on the fly, links hot pages by shared editors and by Wikidata identity across languages, and runs
FalkorDB's WCC to find story components and PageRank to find each one's epicenter. No LLM is involved yet."

Open FalkorDB Browser (localhost:3001) for five seconds, run:
`MATCH (u:Editor)-[:MADE]->(e:Edit)-[:ON]->(p:Page) WHERE e.ts > timestamp()/1000 - 1200 WITH p, count(DISTINCT u) AS n WHERE n >= 3 RETURN p.title, n ORDER BY n DESC`

## 1:00 — The crew (Crew tab)
"The Scout triages components against memory: Story nodes already in the graph. It checks the graph before spending a
token. New clusters become a Story plus a Task node. That Task *is* the hand-off: an Analyst claims it with a single
Cypher mutation."

"The Analyst pulls the dossier, reads the actual diffs from Wikipedia, queries the graph when it needs more, and files a
Brief. Every claim gets a CITES edge to the exact revision it rests on."

## 1:45 — A story (Stories tab)
Open a filed story. Read the headline, show confidence, click a cited revision to open the real diff on Wikipedia.
"This brief came from the shape of the edits, not from a news feed."

## 2:15 — Ask the Desk
Type: "what's happening right now, and is any of it edit-warring?" Show the tool calls streaming, then the answer with
clickable [story:] and [rev:] citations.

## 2:45 — Close
"Pull FalkorDB and nothing works: detection, memory and coordination are all Cypher. Seismo, built solo for Graph Hacks."
