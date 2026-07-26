# Final whole-branch review — findings

Branch `feat/phase-1-crawler`, 19 commits, 90 tests passing. Reviewed 2026-07-26 after all ten
tasks completed and passed their individual reviews.

**The crawler has never run against the live site.** The Cloudflare-through-VPN probe (Task 1
Step 12) is still pending credentials, so every claim here about network behaviour is reasoned
from code, not observed.

## Critical

### C1 — The archive is write-once, and the plan traded away the fallback that assumed otherwise

`filter_unseen` treats `status='ok'` as final regardless of `retry_errors`, nothing re-offers an
`ok` article, and there is no refetch command. Errored IDs fare no better: `run_backfill` advances
its cursor unconditionally and only ever walks downward, so an ID that errored in a completed batch
is never seen again. The bounded retry added in the last commit is reachable only for the poller
and for a crash inside an unfinished batch.

A transient 5xx at 0.1% over 2.8M IDs silently loses ~2 800 articles. Worse, a markup change on day
20 writes millions of degraded rows with no way back — and the plan justified dropping the spec's
30-day raw-HTML window on exactly the grounds that articles "remain re-fetchable, and `fetch_log`
records which IDs those are". That capability was never built, so both mitigations for the spec's
own headline risk ("the parser will break") are missing.

Fix: an error-sweep phase in `run_backfill` over `fetch_log WHERE status='error' AND attempts < 5`
(the index exists), plus `babel refetch --ids/--range` that clears `fetch_log` rows. Until one
exists, restore the raw-HTML window.

## Important

**I1 — `pending` image rows survive any unclean shutdown.** `fetch_log='ok'` is committed before
the image loop, and roughly 86% of wall-clock sits inside that loop. No signal handler, so
`docker stop` strands rows on essentially every restart, and `filter_unseen` never re-offers them.
Fix: a startup sweep over `article_images WHERE status='pending'`, or move `record_fetch(ok)` after
the loop.

**I2 — Image fetches share the eRepublik rate limiter, so the crawl estimate is ~7× wrong.**
SPEC.md's "~32 days at 1 req/s" counts article requests only. At 6.2 images each the real figure is
~20M requests, about **234 days**. The politeness budget exists for erepublik.com; throttling
imageshack at the same global rate serves nothing. Either give images their own limiter or correct
the spec — right now the two contradict each other.

**I3 — A hardcoded User-Agent overrides curl_cffi's impersonation.** `fetcher.py` sets
`Chrome/131.0.0.0` on a session created with `impersonate="chrome"`, whose own header set carries a
much newer Chrome. A UA disagreeing with the TLS fingerprint is a primary bot-detection signal, and
it is applied inconsistently — the image and RSS getters don't set it. **This must go before the
probe runs**, or the probe measures a self-inflicted handicap.

**I4 — After the backfill finishes, the service can never run again.** The cursor persists at 0, so
the next start returns immediately, `FIRST_COMPLETED` cancels the poller, and the container
crash-loops under `restart: unless-stopped`. Fixing C1's sweep loop fixes this too.

**I5 — The egress watchdog dies on a lookup failure, not only on a leak.** `check_ip_leak` raises a
bare `RuntimeError` when both IP providers fail — and `vpn.py` itself notes they rate-limit and that
Gluetun's DNS blocks some of them. That escapes `except IpLeak` and takes down a month-long crawl.
The shared network namespace already makes a leak impossible; a failed lookup is not evidence of
one. Tolerate N consecutive failures.

**I6 — Image bodies are fully buffered before the size cap applies.** The cap is checked after
download, and image URLs point at arbitrary author-chosen hosts. On a 7.4 GiB Jetson also running
Postgres, a large file is an OOM. Stream with an abort at `max_image_bytes`, or reject on
`Content-Length`.

**I7 — The operator's home country was in committed test code.** `tests/test_vpn.py` used a real
two-letter country code, mirrored in the plan document, while `config.py` correctly defaults to the
placeholder `XX`. This is precisely the fact the VPN exists to hide, in a public repo. Fixed:
placeholders `AA`/`ZZ` throughout.

## Minor

- Connection pool sits exactly at its limit (`max_size=4`, four consumers). One more hangs the
  process silently.
- `article_images` position remap can pair a URL with a stale hash if a re-parse shifts the image
  list. Only reachable once C1's refetch path exists — fix them together.
- Both producers can claim the same ID on the newest ~50. Wasted requests only; `save_article` is
  idempotent.
- The plan document embeds an absolute `/Users/<name>/...` path, in a public repo.
- `IMAGE_ROOT` misconfiguration is silent: a missing `.env` sends `have_space` to the container
  overlay, and every image becomes `skipped_no_space` with no warning.
- Dead config: `gluetun_api_url` is never read (rotation deferred). `images.width`/`height` and
  `articles.lang` are created but never written — the plan records those deviations, SPEC.md does
  not.
- No observability beyond `poll ingested N`: no status distribution, no `skipped_no_space` rate, no
  cursor progress over what is now a very long run.

## Triage of the seven Minors deferred from per-task reviews

- **Stale, no action:** the `filter_unseen` f-string finding — it now uses bound parameters.
- **Void:** the fixture-scope wording in a report file that is not committed.
- **Harmless:** comment `position` gaps (position is only ever used for ordering); the weak
  timezone assertion (`test_eday_to_date` already pins the off-by-one the spec cares about).
- **Worth doing while nearby:** loosen the `<0.05s` timing bound in
  `test_first_acquire_is_immediate`.
- **Promote:** the orphan `article_images` rows finding understates itself — see the URL/hash remap
  above.
- **Accepted trade:** `DOT=off`. Service-name resolution requires it and queries still leave through
  the tunnel; no identity leak.

## Recommended order

Before merge: C1, I1, I3, I4, I5, I7. I3 and I7 are minutes of work and I3 gates the probe's
validity. I2 needs a decision rather than code. I6 can follow the first short live run.

Nothing here is validated until the Cloudflare-through-VPN probe has actually run.
