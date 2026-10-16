# Audiobook Catalog

[![CI](https://github.com/eugeniobergia/audiobook/actions/workflows/ci.yml/badge.svg)](https://github.com/eugeniobergia/audiobook/actions/workflows/ci.yml)

A REST API for an audiobook catalog. It ingests audiobooks from the
[iTunes Search API](https://performance-partners.apple.com/search-api), cleans
them, computes a color palette from each cover, stores them in PostgreSQL, and
lets clients browse, search, get stats and export the catalog.

Design decisions and trade-offs are in [NOTES.md](NOTES.md).

## Quick start

You need [Docker](https://docs.docker.com/get-docker/) with Docker Compose.

```bash
git clone https://github.com/eugeniobergia/audiobook.git && cd audiobook
cp .env.example .env          # the defaults work as they are
docker compose up --build
```

Then open **http://localhost:8000/docs**, click **Authorize**, enter the API
key (`change-me`, from `.env`), and try the endpoints.

The catalog starts empty: run an ingestion first (see below).

To stop: `docker compose down`. To also delete the stored data:
`docker compose down --volumes`.

## Configuration

Settings come from environment variables, or from the `.env` file.
`.env.example` documents each one.

| Variable | Required | Default | What it is |
|---|---|---|---|
| `API_KEY` | yes | | Key clients send in the `X-API-Key` header |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | yes | | Database credentials, shared by the app and the Postgres container |
| `POSTGRES_HOST`, `POSTGRES_PORT` | no | `localhost`, `5432` | Where the app finds Postgres (Docker Compose sets the host to `db`) |
| `HOST`, `PORT` | no | `localhost`, `8000` | Where the API listens (Docker Compose sets `0.0.0.0`) |
| `DB_POOL_MAX` | no | `10` | Most database connections the app opens |
| `ITUNES_BASE_URL` | no | `https://itunes.apple.com` | iTunes Search API location |
| `ITUNES_TIMEOUT_SECONDS` | no | `10` | Timeout for iTunes requests |
| `PALETTE_CONCURRENCY` | no | `10` | Cover images downloaded at once during ingestion |

The app refuses to start if a required variable is missing.

## Using the API

Every endpoint except `GET /health` and the docs needs the API key. Without it,
the API answers `401`. Errors always have the same shape:
`{"error": {"code": "...", "message": "...", "details": ...}}`.

| Method and path | What it does |
|---|---|
| `GET /health` | Health check (public): 200 if the app is up and the database answers, else 503 |
| `POST /ingestions` | Fetch audiobooks from iTunes, clean and store them |
| `GET /audiobooks` | List audiobooks: `q` (title or author), `genre`, `limit`, `offset` |
| `GET /audiobooks/{id}` | One audiobook, by its iTunes collection id |
| `GET /stats` | Counts by genre and by release year |
| `GET /audiobooks/export` | The whole catalog, streamed as NDJSON |

The examples below assume the key from `.env.example`:

```bash
export API_KEY=change-me
```

### Ingest audiobooks

```bash
curl -X POST http://localhost:8000/ingestions \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"term": "dune", "limit": 25}'
```

```json
{"source": "itunes", "fetched": 30, "created": 30, "updated": 0, "skipped": 0, "invalid": []}
```

- **Search parameters:** `term` (required), `limit` (1–200, default 50),
  `country` (two-letter store code, default `US`) and `attribute` (`titleTerm`,
  `authorTerm`, `genreIndex` or `ratingIndex`). They're passed to iTunes as
  they are; iTunes sometimes returns a few more results than `limit`, as above.
- **Safe to repeat:** running the same ingestion again creates nothing new.
  Unchanged records count as `skipped`, and changed ones as `updated`.
  `fetched == created + updated + skipped` always holds.
- **Bad records** are skipped and listed in `invalid` with the reason.

**If iTunes is unavailable,** ingestion falls back to a saved sample of real
iTunes data, and the summary says `"source": "fallback"`. To see it, point the
app at an address where nothing answers:

```bash
ITUNES_BASE_URL=http://localhost:9 docker compose up -d
```

Run `docker compose up -d` again (without the variable) to go back to iTunes.

### Browse, search and stats

```bash
curl "http://localhost:8000/audiobooks?q=herbert&limit=5" -H "X-API-Key: $API_KEY"
curl "http://localhost:8000/audiobooks?genre=sci-fi%20%26%20fantasy" -H "X-API-Key: $API_KEY"
curl http://localhost:8000/audiobooks/1441611591 -H "X-API-Key: $API_KEY"
curl http://localhost:8000/stats -H "X-API-Key: $API_KEY"
```

The list is sorted by title. Its `total` counts every match, so clients can
work out how many pages there are.

### Export

```bash
curl http://localhost:8000/audiobooks/export -H "X-API-Key: $API_KEY" -o audiobooks.ndjson
```

It streams one JSON object per line, with the same fields as the list items,
ordered by id. The data is read from the database in batches as it's sent, so
memory stays flat however large the catalog is.

## Development

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and
Docker (for Postgres). uv installs the right Python version by itself.

```bash
uv sync                       # install dependencies, dev tools included
docker compose up -d db       # just the database
uv run audiobook              # the API, from your machine, at http://localhost:8000
```

When the API runs on your machine (not in Docker), `.env` must keep
`POSTGRES_HOST=localhost`, as in `.env.example`.

### Tests and checks

The tests need the Postgres container running (`docker compose up -d db`).
They create and use a separate `audiobook_test` database, so your data isn't
touched. If Postgres isn't running, the database tests fail with a message
saying so, rather than being skipped.

| Command | What it does |
|---|---|
| `make check` | Everything CI runs: lint, type check, tests |
| `make test` | Tests only (`pytest`) |
| `make lint` | Lint and formatting check (`ruff`) |
| `make typecheck` | Strict type check (`mypy`) |
| `make format` | Fix lint issues where possible, and format the code |

Upstream services (iTunes, cover images) are faked in the tests, so they run
without network access.

### CI

GitHub Actions ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs on
every push to `main` and every pull request:

- **Checks:** lint, type check and tests, against a Postgres service container.
- **Docker smoke test:** starts the stack from `.env.example` with
  `docker compose up` and calls the API, the same way the quick start does.

## Project layout

```
src/audiobook/
├── api/            # FastAPI app, routes, auth, error handling, response schemas
├── clients/        # iTunes Search API client, plus the fallback sample
├── services/       # ingestion, normalization (cleaning), cover palettes
├── repositories/   # SQL (raw psycopg)
├── db/             # connection pool, schema.sql
├── models.py       # the Audiobook model shared by every layer
└── config.py       # settings from environment variables
tests/              # pytest; database tests use a real Postgres
```
