# Insight

Drop in a spreadsheet and get the dashboard an analyst would build: the data
cleaned and explained, the headline numbers, the trend, what drives the
outcome. Every chart comes with a sentence that says what it shows. A pattern
becomes a **key finding** only if it passes a significance test.

This is the Python rebuild of `insight-executive-v2`: one FastAPI service,
Neon Postgres for storage, Redis for caching, and a server-rendered interface
(htmx and Plotly) with no front-end build step.

## What's in the product

| Page | What it does |
| --- | --- |
| **Landing, Pricing** | Public marketing pages, and live demos on three sample datasets that need no sign-up |
| **Get data** | Drag-and-drop upload with progress (CSV, TSV, Excel, JSON, Parquet), import from a link (Google Sheets, GitHub, Dropbox, any URL), or start from a sample |
| **Dashboard** | A headline, KPI cards with sparklines and period-on-period change, key findings, and 4 to 8 charts chosen and captioned by the engine. Filter the whole board by year or category without reloading |
| **Ask a question** | Plain English in, chart and sentence out ("top 10 products by revenue in 2025"), plus a sandboxed SQL console (Pro) |
| **Data** | The cleaned rows, searchable and paginated, with a CSV export |
| **Cleaning report** | Every change made on the way in, and how each column is read (summed, averaged, split by, ignored) |
| **Library** | Saved analyses (a board plus its filters), and those shared with you |

Plans: **Starter** (free: 5 datasets, 10 MB files, 3 saved analyses) and
**Pro** (unlimited, 100 MB files, sharing, SQL console). Limits are defined
once in `services/plans.py` and enforced on the server.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
uvicorn insight.main:app --reload        # http://localhost:8000
```

With no configuration it uses a local SQLite file and an in-process cache.
For the full production shape locally (Postgres plus Redis):

```bash
docker compose up --build
```

## Configuring Neon and Redis

Copy `.env.example` to `.env` and set:

```bash
# Neon console → Connect → the *pooled* connection string ("-pooler" in the host)
DATABASE_URL=postgresql://user:pass@ep-xxxx-pooler.eu-central-1.aws.neon.tech/insight?sslmode=require
# Same database without "-pooler", used by migrations
MIGRATION_DATABASE_URL=postgresql://user:pass@ep-xxxx.eu-central-1.aws.neon.tech/insight?sslmode=require
REDIS_URL=rediss://default:pass@your-redis.upstash.io:6379
AUTO_CREATE_TABLES=false
ENVIRONMENT=production
```

Then apply the schema with `alembic upgrade head`. The Docker image does this
on start.

## Deploy to Vercel

The repository is ready for Vercel's Python runtime. `app.py` is the entry
point, `vercel.json` places the function in `iad1` (next to Neon's us-east-1),
and the function bundle is about 225 MB. That fits Vercel's limit because the
app needs neither SciPy nor pyarrow (see docs/ARCHITECTURE.md).

1. **Import the repository** in Vercel (Add New → Project). It detects FastAPI;
   leave the build settings as they are.
2. **Connect Neon.** Either add the Neon integration from the Vercel
   Marketplace, which sets `DATABASE_URL` and `DATABASE_URL_UNPOOLED`, or add
   them yourself under Settings → Environment Variables: the pooled string as
   `DATABASE_URL`, the direct one as `MIGRATION_DATABASE_URL`.
3. **Add Redis.** Add Upstash for Redis from the Marketplace (it sets
   `KV_URL`, which is read as the cache URL), or set `REDIS_URL`. Without it
   every function instance keeps its own small cache, which works but is far
   less effective.
4. **Set** `ENVIRONMENT=production`, `AUTO_CREATE_TABLES=false`,
   `BASE_URL=https://your-domain` and `CONTACT_EMAIL`.
5. **Migrations** run outside the deploy: `.github/workflows/migrate.yml`
   applies them to Neon on every push to `main` that changes them (add the
   direct connection string as the `MIGRATION_DATABASE_URL` repository
   secret). Or run `alembic upgrade head` yourself.

What changes on Vercel (it sets `VERCEL=1`, and the app reads it):

- **No local connection pool.** Each request borrows a connection from
  Neon's pooler.
- **Secure cookies on every deployment**, previews included.
- **Uploads are capped at 4 MB**, because Vercel Functions accept at most
  4.5 MB per request. Larger files can still be imported from a link. Taking
  big uploads directly would mean sending them to Vercel Blob from the
  browser, which isn't built yet.
- **Static files** are served with content-hashed URLs and `immutable`
  cache headers, so Vercel's CDN serves them after the first request.

## Project layout

```
src/insight/
  engine/        the analysis: pure functions over pandas, no I/O
    cleaning.py    what each value is (numbers, dates, categories, redaction)
    fields.py      what each column is (time, measure, category, id, outcome)
    measures.py    the numbers a business talks about
    query.py       the one way numbers are computed (a Spec, run over the rows)
    stats.py       the tests behind "rose", "highest", "goes with"
    captions.py    the sentence under each chart
    planner.py     which charts to show
    render.py      compute a tile or the whole board, under filters
    ask.py         a question in plain words → a tile
    sql_console.py read-only, sandboxed DuckDB
  ingest/        file readers, and safe fetching of links
  db/            SQLAlchemy models and the Neon-aware engine
  cache/         Redis or in-memory cache, cache keys, the in-process table cache
  services/      use cases: auth, datasets, insights, library, plans
  web/           FastAPI routes, Jinja templates, CSS/JS, security middleware
  samples/       the bundled demo datasets (scripts/make_samples.py)
migrations/      Alembic
tests/           pytest: engine, cleaning, infrastructure, end-to-end web
docs/ARCHITECTURE.md   the system design and its trade-offs
app.py, vercel.json    the Vercel entry point and function settings
```

## Checks

```bash
ruff check . && ruff format --check .
pytest -q                                   # SQLite and the in-memory cache
TEST_DATABASE_URL=postgresql://… TEST_REDIS_URL=redis://… pytest -q   # real Postgres and Redis
```

CI (`.github/workflows/ci.yml`) runs lint, both test configurations and the
migrations on every push.
