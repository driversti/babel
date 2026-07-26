# CLAUDE.md

Archive of every eRepublik article and its comments, eventually searchable across languages.

**Read [SPEC.md](SPEC.md) first** — it holds the design, the measured facts about the source
(feed limits, page structure, volume, sizing) and the phase boundaries. Those measurements were
expensive to obtain; do not re-derive them, and update them there if the site changes.

## Status

Phase 1 — crawler. No code yet.

## Key facts

- Article pages are public: `GET /en/article/{id}/1/1000` needs no slug and no session, and
  returns the article plus all comments in one request.
- The RSS feed caps at 5 pages (~3 days). It is for live polling only; history comes from
  walking article IDs downward.
- Whole archive is ~2.8M articles, ~9.6 GB of text. Current publication rate is ~18/day.
- Crawl politely: start at 1 req/s.

## Commands

To be filled in once the project has code.
