# Submission text (paste into the WeMakeDevs panel)

**Project name:** Seismo

**Tagline:** World events, felt through Wikipedia's live edit graph.

**Tracks:** 01 Agents That Act on Connected Data · 02 Agent Memory and Coordination

**Repo:** https://github.com/divyanshkhurana06/seismo

**Demo video:** <paste link>

**Live deployment:** <paste link, or "local setup, see README (Docker + one command)">

**Team:** Divyansh Khurana (solo)

## Description

News doesn't get written on Wikipedia. It gets edited. When something happens in the world, strangers who have never
coordinated converge on the same handful of articles within minutes, often in several languages at once. That convergence
has a shape, and the shape appears before any headline does.

Seismo is a newsroom whose only sense organ is that shape. It streams every human edit on every Wikipedia (about two per
second) into FalkorDB as a live graph of pages, editors and edits. Every 45 seconds, pure Cypher finds pages where unusually
many distinct humans just converged; a projection is built on the fly (hot pages linked by shared low-footprint editors and
by Wikidata identity across languages), and FalkorDB's WCC finds story components while PageRank finds each one's epicenter.
No LLM is involved in detection.

A crew of Claude agents then acts on the graph. The Scout checks graph memory first (is this cluster already a Story? is a
Task open?) and only then decides new / update / ignore, creating Story and Task nodes. An Analyst claims a Task with a
single Cypher mutation, reads the dossier from the graph, pulls the actual diffs from Wikipedia, runs its own read-only
Cypher, and files a Brief whose every claim has a CITES edge to the exact revision it rests on. The Desk answers questions
from the graph with clickable [story:] and [rev:] citations. Restart the process and the crew remembers everything.

In its first minutes on the live stream, Seismo filed a cited, 75%-confidence brief on an explosion at CFB Wainwright in
Alberta while correctly dismissing a vandalism spree on a list page as "not news".

Why FalkorDB has to be there: detection, hand-offs and memory are all Cypher and graph algorithms. Remove the graph and
there is no product.

**Graph model, all Cypher queries, and setup are in the README.**

## AI assistance disclosure

An AI coding assistant (Claude) was used during the hackathon window. Design, graph model, and all decisions are mine.
