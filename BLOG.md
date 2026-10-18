# I built a newsroom that can't read the news (draft for dev.to)

*Seismo: agents that sense world events through the topology of Wikipedia's live edit graph, on FalkorDB.*

## The idea

Every editor-bot pipeline I've seen that "detects breaking news" reads headlines. I wanted to know whether you could detect
an event *without any text at all*, from pure structure. Wikipedia gives you a signal nobody else has: a public, real-time
stream of every edit on every language edition. When a plane goes down, strangers who have never spoken converge on the
airline's page, the airport's page, the aircraft type's page, in a dozen languages, within minutes. The edits are not the
signal. The *convergence* is.

That is a question about paths between people and pages, which is a graph question. So the whole thing runs on FalkorDB.

## The graph

Three node types do the heavy lifting: `Page`, `Editor`, `Edit`, with `(:Editor)-[:MADE]->(:Edit)-[:ON]->(:Page)`.
Anonymous editors arrive as IP addresses; I hash them before anything is stored. A `TOUCHED` edge between editor and page is
kept up to date on every write so the detectors never have to re-aggregate history.

## Detecting a tremor

Every 45 seconds:

1. One Cypher aggregation finds pages with three or more distinct humans in the last twenty minutes.
2. Those pages get a temporary `:Hot` label. `CO_EDITED` edges are created between hot pages that share an editor,
   **excluding editors who touched more than four hot pages** (recent-changes patrollers would otherwise glue the whole
   graph into one blob; that bug cost me an hour and was the most instructive moment of the build).
3. `CALL algo.WCC(...)` over `CO_EDITED` and `SAME_AS` gives story components. `SAME_AS` links pages that resolve to the
   same Wikidata item, which is how a cluster can span languages.
4. `CALL algo.pageRank('Hot', 'CO_EDITED')` picks the epicenter.
5. Labels and edges are removed again. The projection lives for a few milliseconds.

No LLM has run yet.

## The crew, and why their memory is a graph

A Scout decides what's worth investigating. Before it spends a token it asks the graph: are these pages already `ABOUT` a
`Story`? Is a `Task` for it `open` or `claimed`? Most cycles that answer is "yes, nothing to do", and the Scout costs
nothing. When it does file, the `Task` node *is* the hand-off: an Analyst claims it with one `SET`.

The Analyst then reads the dossier from the graph, pulls real diffs from Wikipedia, writes its own read-only Cypher, and
files a `Brief`. Every revision it relied on becomes a `CITES` edge. That makes the output auditable in the most literal way:
you can click from a claim to the exact diff on Wikipedia.

Twenty minutes after I turned it on, the crew filed a 75%-confidence brief on an explosion at a Canadian military base,
while dismissing a vandalism spree on "List of Australian hip-hop musicians" as "not news". It was right about both.

## What I'd do next

Edit-war detection as a path pattern (A reverts B reverts A), a per-language baseline so quiet wikis aren't drowned by
English, and a longer memory so the crew can say "this page last tremored in March, for the same reason".

Repo: https://github.com/divyanshkhurana06/seismo
