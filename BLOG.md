# I built a newsroom that is not allowed to read the news

*Built solo in a weekend for Graph Hacks 2026 (WeMakeDevs × FalkorDB). Repo: https://github.com/divyanshkhurana06/seismo · Live: https://seismo.onrender.com*

I spent this weekend on Graph Hacks, the WeMakeDevs and FalkorDB hackathon, and I want to write down what I made while it is still fresh, because the idea surprised me more than I expected.

## Where it started

Every "breaking news detector" I have ever seen reads text. It scrapes headlines, it watches tweets, it runs sentiment on something. Fine. But I kept thinking about a different signal that nobody seems to use: **Wikipedia publishes a live feed of every single edit on every language edition, in real time, for free.** Roughly two human edits a second, all day, forever.

Here is the thing about that feed. When something actually happens in the world, strangers who have never spoken to each other show up on the same handful of articles within minutes. A plane goes down and suddenly the airline page, the airport page and the aircraft model page all light up, in a dozen languages. Nobody coordinated that. It just happens.

**The edits themselves are not the signal. The convergence is.** And convergence is a question about who touched what, together, right now. That is a graph question. So I decided the whole project would live inside a graph database, and the agents would not be allowed to read a single headline. Everything they know, they have to learn from the shape of the edits.

I called it **Seismo**. It felt right. You are not watching the earthquake, you are watching the needle twitch.

## What the graph looks like

I kept the model small on purpose because I only had a few hours. Three main node types: `Page`, `Editor` and `Edit`.

```
(:Editor)-[:MADE]->(:Edit)-[:ON]->(:Page)
```

That is basically it. Anonymous editors come through the feed as IP addresses, which made me uncomfortable, so I hash them with a salt before anything touches the database. **No IP is ever stored.**

One extra edge turned out to matter a lot. Every time an editor touches a page I also update a TOUCHED relationship between them with a counter and a timestamp. It sounds redundant but it means the detectors never have to crawl through thousands of edit nodes to answer "who has been on this page lately".

## Finding a tremor without any AI

Every 45 seconds the detector runs, and it is **pure Cypher plus FalkorDB's built in algorithms**. No language model anywhere in this part.

First, one aggregation finds pages where three or more different humans edited in the last twenty minutes:

```cypher
MATCH (u:Editor)-[:MADE]->(e:Edit)-[:ON]->(p:Page) WHERE e.ts > $since
WITH p, count(e) AS edits, count(DISTINCT u) AS editors
WHERE editors >= 3
RETURN p.title, editors, edits
```

Then those pages get a temporary Hot label and I build a projection on the fly: an edge between any two hot pages that share an editor in that window, plus an edge between pages that resolve to the same Wikidata item in different languages. Then **WCC** finds the connected components, which become story candidates, and **PageRank** picks the epicenter of each one.

```cypher
CALL algo.WCC({nodeLabels: ['Hot'], relationshipTypes: ['CO_EDITED', 'SAME_AS']})
YIELD node, componentId
```

After that the labels and the temporary edges get deleted. The projection exists for a few milliseconds and then it is gone.

The first time I ran it, the top cluster was a list of Australian hip hop musicians, a town in the Falkland Islands, an Indian actor and a 2010 action film. Obviously not one story. It took me a while to see it: **one person doing vandalism patrol had reverted something on every single one of those pages**, and my projection treated that as "these pages are related". So now editors who touch more than four hot pages in a window do not get to create edges. Patrollers are hubs, and hubs cannot glue stories together. That one fix was the most instructive hour of the whole build.

## The crew, and why their memory is also a graph

Once the detector has candidates, three Claude agents take over. I wanted the coordination between them to live in the database too, not in some Python dictionary that disappears when the process restarts.

**The Scout** looks at the tremor components and decides what is worth investigating. Before it spends a single token it asks the graph: are these pages already attached to a Story? Is there an open Task for it? Most of the time the answer is yes and the Scout does nothing, which keeps the cost down. When it does decide something is new, it creates a Story node and a Task node. **That Task is the hand off.** An Analyst claims it with one SET statement, and the UI shows the status flip from open to claimed to done.

**The Analyst** is the interesting one. It pulls a dossier straight from the graph, reads the actual diffs from Wikipedia, writes its own read only Cypher when it wants to know more, and files a Brief. Every revision it relied on becomes a CITES edge from the brief to that exact Edit node. So you can click from a sentence in the brief to the real diff on Wikipedia and check it yourself. I like that a lot. **Auditability is not a feature I bolted on, it is just what the data model does.**

There is also **a Desk agent** you can ask questions, and it answers from the graph with citations that the UI turns into links.

## The moment it worked

About twenty minutes after I turned the crew on against the live stream, it filed its first real brief. Three editors had converged on a page about an explosion and fire at a Canadian military base in Alberta. The Analyst read the diffs, noticed that one edit raised the death toll based on a bare video link while the existing citation only said "multiple fatalities", and wrote that down as a reason for caution. It gave the story **75 percent confidence** and called it developing.

In the same run it looked at that hip hop list page with all the reverts and said, roughly, this is two temporary accounts adding themselves and a patroller cleaning up, **not news**. It was right about both. I sat there for a second and just grinned.

## What I would do next

Edit wars as an actual path pattern, A reverts B reverts A, instead of just counting reverts. A per language baseline so quiet wikis do not get drowned out by English. And a longer memory, so the crew can say "this page last tremored in March, for the same reason". Right now it keeps a few hours of edits and the stories forever, which was the right trade for a weekend.

If you want to poke at it, the repo is at **https://github.com/divyanshkhurana06/seismo** and there is a live instance at **https://seismo.onrender.com**. It is on a free tier so give it thirty seconds to wake up. Pull FalkorDB out of it and nothing works, which is exactly how I wanted it.
