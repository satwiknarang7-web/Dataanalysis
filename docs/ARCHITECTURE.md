# Architecture

## Shape of the system

```
Browser ── HTML + htmx partials, Plotly charts
   │
   ▼
FastAPI (uvicorn, N workers × M containers, stateless)
   ├── web/        routes, templates, security middleware
   ├── services/   auth, datasets, insights, library, plans
   ├── engine/     pure pandas/numpy/scipy; runs on a bounded thread pool
   │
   ├── L1  FrameCache        in-process LRU of parsed tables (by content hash)
   ├── L2  Redis             plans, boards, answers, sessions, rate limits
   └──     Neon Postgres     users, sessions, datasets, analyses, shares, Parquet blobs
```

The app servers hold no state that matters: sessions live in Postgres
(cached in Redis), and everything computed lives in Redis. Any instance can
serve any request, so scaling out means adding containers behind a load
balancer.

## Why these choices

**Neon Postgres** is the system of record. The data is relational (users own
datasets, analyses point at datasets, shares point at analyses) and needs
real constraints: unique emails, cascading deletes, foreign keys. Neon adds
serverless scaling and branching, which suit an early product: a branch per
pull request costs nothing when idle. Three Neon details are handled in
`db/session.py`:

- **TLS** is required.
- The **pooled endpoint** is PgBouncer in transaction mode, which can't keep
  prepared statements between transactions, so both asyncpg's and
  SQLAlchemy's statement caches are off.
- Neon **suspends idle computes** and drops their connections, so each
  connection is pinged on checkout (`pool_pre_ping`) and recycled every five
  minutes.

**Rows as Parquet blobs, addressed by content.** A cleaned table is stored
once, as zstd-compressed Parquet (3–4× smaller than the CSV on the samples), in
`dataset_blobs`, keyed by its SHA-256. Uploading the same file twice stores
it once. The insert is `ON CONFLICT DO NOTHING`, so two identical uploads at
once can't collide. Metadata lives in `datasets`, so listing a library never
reads a blob. When files outgrow Postgres (hundreds of MB), only
`dataset_blobs` moves to object storage (S3 or R2); the content hash stays
the key and nothing else changes.

**Caching in three tiers, all content-addressed.**

| Tier | What | Where | Invalidation |
| --- | --- | --- | --- |
| L1 | Parsed DataFrames | In-process LRU (`cache/frames.py`) | None needed: keyed by content hash |
| L2 | Board plans, rendered boards per filter set, answers | Redis (`cache/backend.py`) | None needed: keys include the content hash, the filters' hash and `ENGINE_VERSION` |
| L2 | Sessions | Redis, 2-minute TTL | Deleted on sign-out |
| L2 | Sign-in attempt counters | Redis `INCR` with expiry | Expire after 15 minutes |

Rows under a content hash never change, so a cached board can never be
stale. Changing the engine's rules means bumping `ENGINE_VERSION`, which
moves every key at once. Measured locally, a board takes about 1 s to
compute cold and about 10 ms from cache.

`get_or_compute` is **single-flight**: when a team opens the same new board
at once, one request computes it and the rest wait for that result. Redis is
an **optimisation, never a dependency**. If it is down, reads become misses,
writes are dropped and rate limits fail open, and the app keeps serving from
Postgres plus compute. Without `REDIS_URL` an in-process TTL cache is used
(fine for one process; use Redis as soon as there are two).

**CPU work stays off the event loop.** Cleaning, planning and rendering run
on a thread pool behind a semaphore (`AppState.run_cpu`). pandas and numpy
release the GIL for most of their work, so threads give real parallelism,
and the semaphore stops a burst of uploads from starving request handling.
If uploads ever dominate, the next step is a job queue (arq or Celery on the
same Redis) with the upload page polling for completion. The service layer
already isolates that work in one function (`datasets.prepare`).

**A server-rendered interface.** Jinja templates, htmx for partial updates
(filters, answers, pagination, sharing) and Plotly for charts. There is no
front-end build, one language end to end, and fast first paint. The charts
are interactive, and the KPI sparklines are SVG drawn on the server. All
JavaScript is self-hosted, so the CSP is `script-src 'self'`.

## The engine, in one paragraph

`cleaning` decides what each value is, and every decision is listed on the
Cleaning report. `fields` decides what each column is, from its values first
and its name second. `measures` names the numbers a business talks about.
`planner` lists the analyses an analyst would consider, computes each over
every row, scores it by what the data shows, and keeps a balanced set of 4
to 8, with no two charts telling the same story. Every number is a `Spec`
run by `query.run`, so a filter re-runs the same spec and a chart can never
disagree with its caption. A caption claims "rose", "highest" or "goes with"
only when the test in `stats.py` backs it: Welch's t and a pooled-proportion
z-test (each group against the rest, Bonferroni-corrected), a regression
slope, and Pearson's r. Otherwise it says plainly that nothing stands out,
and it never becomes a key finding. `tests/test_engine.py` checks that the
planted patterns in the samples are found and that pure noise produces no
findings.

## Security

- **Passwords:** Argon2id. **Sessions:** a random 256-bit token in an
  HttpOnly, SameSite=Lax cookie (Secure in production). Only its SHA-256 is
  stored.
- **Cross-site requests:** writes must come from the same origin (checked
  from the Origin or Referer header), on top of SameSite cookies.
- **Headers:** strict CSP, `X-Frame-Options: DENY`, `nosniff`, HSTS in
  production.
- **Access:** a dataset or analysis that isn't yours, and isn't shared with
  you, returns 404, not 403, so its existence isn't revealed.
- **Personal data:** emails and phone numbers are redacted during cleaning,
  before anything is stored.
- **Link import:** SSRF guards refuse private, loopback, link-local and
  metadata addresses on every redirect hop, follow redirects by hand, cap
  the body at 50 MB, and time out.
- **SQL console:** a fresh in-memory DuckDB per query that can see one
  table, with file, network and extension access disabled and the
  configuration locked. Only a single `SELECT` is accepted, results are
  capped and queries are interrupted after 10 seconds.
- **Sign-in:** rate limited per email and IP, with the same hashing work for
  unknown accounts so response timing doesn't reveal which emails exist.
- **Redirects:** `next` must be a path on this site, so sign-in can't be
  used as an open redirect.

## Not yet built

Billing (Pro is assigned in the database; the pricing page links to sales),
email verification and password reset (these need an email provider), the
database and warehouse connectors of the original, maps, PDF, Word and
PowerPoint exports, and an optional language model for rewriting captions.
Each fits the current structure: a connector produces a DataFrame for
`datasets.ingest`, and an export reads a `Dashboard`.
