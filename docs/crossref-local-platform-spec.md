# Crossref Local Platform — Implementation Specification

## Purpose

This document is a complete implementation specification for an AI agent.
It describes a new Git repository `aletheia-probe-crossref-platform` that
mirrors the Crossref journal-metadata API into a local PostgreSQL database
and exposes it via the same adapter pattern used by the existing
`aletheia-probe-openalex-platform` and `aletheia-probe-opencitations-platform`
repositories.

Once deployed, the `crossref_analyzer` backend inside `aletheia-probe` will
query local PostgreSQL instead of `api.crossref.org`, eliminating all HTTP
rate-limit errors during large-scale mass-eval runs (~25 M BibTeX entries).

---

## Background and motivation

`aletheia-probe` contains a backend named `crossref_analyzer`
(`src/aletheia_probe/backends/crossref_analyzer.py`).  It calls
`GET https://api.crossref.org/journals/{issn}` to obtain journal-level
metadata quality indicators (ORCID coverage, funding coverage, licence
coverage, DOI counts by year, etc.) and uses them to assess whether a journal
is legitimate or predatory.

Under parallel mass-eval loads the polite-pool rate limit (≈50 req/s) is
easily exceeded, causing cascading HTTP 429 errors and retry storms.

Crossref exposes a paginated bulk endpoint `GET /journals?rows=1000&cursor=*`
that returns every registered journal (~50 000 entries) with exactly the same
fields as the per-ISSN endpoint.  Downloading that once (or monthly) into
PostgreSQL removes the dependency on the live API entirely.

---

## Reference implementations

Study these before writing any code — the new repo must follow the same
conventions:

| Repo | Key files |
|------|-----------|
| `aletheia-probe-openalex-platform` | `provider/db.py`, `provider/local.py`, `adapter/aletheia_openalex_adapter/client.py` |
| `aletheia-probe-opencitations-platform` | `adapter/aletheia_opencitations_adapter/pool.py`, `adapter/aletheia_opencitations_adapter/local.py` |

Both repos are in `/home/ubuntu/AP/` on the build server and are available
as reference material.  Do **not** copy them blindly — adapt the patterns to
the Crossref data model.

---

## Repository layout

```
aletheia-probe-crossref-platform/
├── loader/
│   ├── pyproject.toml          # standalone CLI tool, not imported by adapter
│   ├── crossref_loader/
│   │   ├── __init__.py
│   │   ├── fetch.py            # paginated download from api.crossref.org/journals
│   │   ├── schema.py           # CREATE TABLE statements
│   │   └── load.py             # upsert into PostgreSQL
│   └── README.md
├── provider/
│   ├── pyproject.toml          # package name: aletheia-crossref-provider
│   ├── provider/
│   │   ├── __init__.py
│   │   ├── db.py               # ThreadedConnectionPool + asyncio.Semaphore gate
│   │   └── local.py            # LocalCrossrefProvider (sync psycopg2)
│   └── README.md
└── adapter/
    ├── pyproject.toml          # package name: aletheia-crossref-adapter
    ├── aletheia_crossref_adapter/
    │   ├── __init__.py         # exports LocalCrossrefAdapter
    │   └── client.py           # async context-manager adapter
    └── README.md
```

---

## PostgreSQL schema

The loader must create and populate these two tables.
Use `snapshot_date DATE NOT NULL` (date the loader ran) for versioning.

```sql
-- One row per journal
CREATE TABLE IF NOT EXISTS crossref_journal (
    id                          SERIAL PRIMARY KEY,
    snapshot_date               DATE        NOT NULL,

    -- Identity
    publisher                   TEXT,
    title                       TEXT,           -- first element of the title array

    -- DOI counts  (from message.counts)
    total_dois                  INTEGER     NOT NULL DEFAULT 0,
    current_dois                INTEGER     NOT NULL DEFAULT 0,
    backfile_dois               INTEGER     NOT NULL DEFAULT 0,

    -- Coverage fractions 0..1  (from message.coverage)
    -- "current" variant preferred when available (message.coverage-type.current)
    orcids_current              REAL        NOT NULL DEFAULT 0,
    funders_current             REAL        NOT NULL DEFAULT 0,
    licenses_current            REAL        NOT NULL DEFAULT 0,
    abstracts_current           REAL        NOT NULL DEFAULT 0,
    affiliations_current        REAL        NOT NULL DEFAULT 0,
    references_current          REAL        NOT NULL DEFAULT 0,
    award_numbers_current       REAL        NOT NULL DEFAULT 0,
    ror_ids_current             REAL        NOT NULL DEFAULT 0,
    similarity_checking_current REAL        NOT NULL DEFAULT 0,

    -- Backfile coverage fractions (from message.coverage)
    orcids_backfile             REAL        NOT NULL DEFAULT 0,
    funders_backfile            REAL        NOT NULL DEFAULT 0,
    licenses_backfile           REAL        NOT NULL DEFAULT 0,
    abstracts_backfile          REAL        NOT NULL DEFAULT 0,
    affiliations_backfile       REAL        NOT NULL DEFAULT 0,
    references_backfile         REAL        NOT NULL DEFAULT 0,
    award_numbers_backfile      REAL        NOT NULL DEFAULT 0,
    ror_ids_backfile            REAL        NOT NULL DEFAULT 0,
    similarity_checking_backfile REAL       NOT NULL DEFAULT 0,

    -- DOIs by issued year: stored as JSONB array of [year, count] pairs
    -- matching message.breakdowns.dois-by-issued-year exactly
    dois_by_year                JSONB       NOT NULL DEFAULT '[]',

    UNIQUE (snapshot_date, id)
);

-- One row per ISSN value (a journal can have print + electronic ISSNs)
CREATE TABLE IF NOT EXISTS crossref_journal_issn (
    issn        TEXT    NOT NULL,
    issn_type   TEXT    NOT NULL,   -- "print" | "electronic" | "link"
    journal_id  INTEGER NOT NULL REFERENCES crossref_journal(id) ON DELETE CASCADE,
    PRIMARY KEY (issn, journal_id)
);

CREATE INDEX IF NOT EXISTS idx_crossref_journal_issn_issn
    ON crossref_journal_issn (issn);
```

---

## Loader (`loader/`)

### Fetch logic (`fetch.py`)

Paginate `GET https://api.crossref.org/journals` using cursor pagination:

```
GET /journals?rows=1000&cursor=*
  → response.message.next-cursor
GET /journals?rows=1000&cursor={next-cursor}
  ...until message.items is empty
```

Required behaviour:
- Set `User-Agent: CrossrefLoader/1.0 (mailto:{email})` — use the polite pool.
  Email is read from env var `CROSSREF_LOADER_EMAIL` (required).
- Rate-limit to at most **5 requests/second** using `asyncio.Semaphore` or
  `asyncio.sleep` to avoid triggering 429s during the bulk download.
- On HTTP 429: honour `Retry-After` response header (seconds); if absent,
  wait 60 s then retry indefinitely.
- On any other HTTP error: retry up to 5 times with exponential backoff
  (1 s, 2 s, 4 s, 8 s, 16 s), then raise.
- Log progress every 5 000 journals to stdout.
- Return an iterator/generator of raw `message.items` dicts — do not buffer
  all 50 000 in memory at once.

### Load logic (`load.py`)

- Accept the iterator from `fetch.py`.
- Open a single psycopg2 connection (DSN from env vars: `PGHOST`, `PGPORT`,
  `PGUSER`, `PGPASSWORD`, `PGDATABASE`).
- For each item, `UPSERT` into `crossref_journal` and replace all rows in
  `crossref_journal_issn` for that journal_id.
- Use `snapshot_date = today()`.
- Commit every 1 000 rows.
- After load completes, `DELETE FROM crossref_journal WHERE snapshot_date < today()`
  to remove stale snapshots.

### Field mapping

Map from the raw Crossref API `message` dict:

| DB column | Source path in message dict |
|-----------|----------------------------|
| `publisher` | `message["publisher"]` |
| `title` | `message["title"][0]` if list else `message["title"]` |
| `total_dois` | `message["counts"]["total-dois"]` |
| `current_dois` | `message["counts"]["current-dois"]` |
| `backfile_dois` | `message["counts"]["backfile-dois"]` |
| `orcids_current` | `message["coverage-type"]["current"]["orcids"]` fallback `message["coverage"]["orcids-current"]` |
| `funders_current` | same pattern for `funders` |
| `licenses_current` | same pattern for `licenses` |
| `abstracts_current` | same pattern for `abstracts` |
| `affiliations_current` | same pattern for `affiliations` |
| `references_current` | same pattern for `references` |
| `award_numbers_current` | same pattern for `award-numbers` |
| `ror_ids_current` | same pattern for `ror-ids` |
| `similarity_checking_current` | same pattern for `similarity-checking` |
| backfile columns | `message["coverage"]["orcids-backfile"]` etc. |
| `dois_by_year` | `json.dumps(message["breakdowns"]["dois-by-issued-year"])` |
| ISSN rows | `message["ISSN-type"]` list of `{"value": "...", "type": "print"\|"electronic"\|"link"}` |

All coverage values are fractions `0..1` — **do not** multiply by 100 in the
loader.  The adapter returns them as fractions; the `crossref_analyzer` backend
multiplies by 100 when it calls `float(raw) * 100.0` (see
`src/aletheia_probe/backends/crossref_analyzer.py` line ~502).

Missing keys must be handled gracefully with `dict.get(..., 0)` / `dict.get(..., [])`.

### CLI entry point

```
crossref-load [--email EMAIL] [--dsn DSN]
```

Both arguments can also be supplied via environment variables.  Print a
one-line summary on completion: `Loaded N journals, M ISSNs. Snapshot: YYYY-MM-DD`.

---

## Provider (`provider/`)

### `provider/db.py`

Identical pattern to `aletheia-probe-openalex-platform/provider/db.py`:

- `POOL_MAX_CONN = int(os.environ.get("CROSSREF_PG_POOL_MAX", "10"))`
- `POOL_MIN_CONN = max(1, min(2, POOL_MAX_CONN))`
- Module-level `ThreadedConnectionPool`, lazily initialised under `threading.Lock`.
- Connection kwargs from `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`,
  `PGDATABASE` — but use separate env vars with a `CROSSREF_` prefix:
  `CROSSREF_PGHOST`, `CROSSREF_PGPORT`, `CROSSREF_PGUSER`,
  `CROSSREF_PGPASSWORD`, `CROSSREF_PGDATABASE`.  This allows the Crossref DB
  to live on a different host than the OpenAlex DB.
- `get_connection()` raises `psycopg2.pool.PoolError` if the pool is exhausted
  (the asyncio.Semaphore in the adapter prevents this in normal operation).
- `release_connection(conn)` calls `putconn`.

### `provider/local.py`

Class `LocalCrossrefProvider` with synchronous methods (psycopg2, no asyncio):

```python
class LocalCrossrefProvider:
    def __init__(self, conn=None) -> None: ...
    def get_journal_by_issn(self, issn: str) -> dict | None: ...
    def close(self) -> None: ...
    def __enter__(self): return self
    def __exit__(self, *_): self.close()
```

`get_journal_by_issn(issn)`:
1. Normalise: `issn.strip().upper()`
2. Query `crossref_journal_issn` JOIN `crossref_journal` WHERE `issn = %s`
   ORDER BY `snapshot_date DESC` LIMIT 1.
3. Return a dict whose keys **exactly match** what the live Crossref API
   returns in `message` — i.e. reconstruct the nested dict structure that
   `crossref_analyzer._extract_journal_metrics()` expects:

```python
{
    "publisher": row["publisher"],
    "title": [row["title"]] if row["title"] else [],
    "counts": {
        "total-dois":    row["total_dois"],
        "current-dois":  row["current_dois"],
        "backfile-dois": row["backfile_dois"],
    },
    "coverage": {
        "orcids-current":              row["orcids_current"],
        "funders-current":             row["funders_current"],
        "licenses-current":            row["licenses_current"],
        "abstracts-current":           row["abstracts_current"],
        "affiliations-current":        row["affiliations_current"],
        "references-current":          row["references_current"],
        "award-numbers-current":       row["award_numbers_current"],
        "ror-ids-current":             row["ror_ids_current"],
        "similarity-checking-current": row["similarity_checking_current"],
        "orcids-backfile":              row["orcids_backfile"],
        "funders-backfile":             row["funders_backfile"],
        "licenses-backfile":            row["licenses_backfile"],
        "abstracts-backfile":           row["abstracts_backfile"],
        "affiliations-backfile":        row["affiliations_backfile"],
        "references-backfile":          row["references_backfile"],
        "award-numbers-backfile":       row["award_numbers_backfile"],
        "ror-ids-backfile":             row["ror_ids_backfile"],
        "similarity-checking-backfile": row["similarity_checking_backfile"],
    },
    "coverage-type": {
        "current": {
            "orcids":               row["orcids_current"],
            "funders":              row["funders_current"],
            "licenses":             row["licenses_current"],
            "abstracts":            row["abstracts_current"],
            "affiliations":         row["affiliations_current"],
            "references":           row["references_current"],
            "award-numbers":        row["award_numbers_current"],
            "ror-ids":              row["ror_ids_current"],
            "similarity-checking":  row["similarity_checking_current"],
        }
    },
    "breakdowns": {
        "dois-by-issued-year": json.loads(row["dois_by_year"]),
    },
}
```

Return `None` if no row found.

---

## Adapter (`adapter/`)

### `adapter/aletheia_crossref_adapter/client.py`

Class `LocalCrossrefAdapter` — async context manager, same duck-type as the
HTTP client used by `CrossrefAnalyzerBackend`.

```python
from provider.db import POOL_MAX_CONN
from provider.local import LocalCrossrefProvider

_conn_semaphore: asyncio.Semaphore | None = None

def _get_conn_semaphore() -> asyncio.Semaphore:
    global _conn_semaphore
    if _conn_semaphore is None:
        _conn_semaphore = asyncio.Semaphore(POOL_MAX_CONN)
    return _conn_semaphore

class LocalCrossrefAdapter:
    async def __aenter__(self) -> "LocalCrossrefAdapter": ...
    async def __aexit__(self, ...): ...
    async def get_journal_by_issn(self, issn: str) -> dict | None: ...
```

**Critical implementation notes** (learned from OpenAlex adapter):

1. In `__aenter__`: first `await _get_conn_semaphore().acquire()`, set
   `self._sem_acquired = True`, then call `LocalCrossrefProvider()` inside
   `loop.run_in_executor(None, ...)`.  If the executor call raises, release
   the semaphore and re-raise.
2. In `__aexit__`: call `provider.close()` **directly** (not via
   `run_in_executor`) — `putconn()` is a fast lock+dict operation, safe on
   the event loop thread.  Then release the semaphore.  Using
   `run_in_executor` for `__aexit__` causes deadlock under load (all executor
   threads blocked on semaphore acquire while no thread is free to run the
   release).
3. `get_journal_by_issn` dispatches `provider.get_journal_by_issn(issn)` via
   `loop.run_in_executor(None, ...)`.

### `adapter/aletheia_crossref_adapter/__init__.py`

```python
from .client import LocalCrossrefAdapter
__all__ = ["LocalCrossrefAdapter"]
```

---

## Integration into `aletheia-probe`

Two changes needed in the existing `aletheia-probe` repository.

### 1. `src/aletheia_probe/backends/crossref_analyzer.py`

Add a factory function `create_crossref_client` (same pattern as
`create_openalex_client` in `src/aletheia_probe/openalex.py`):

```python
def create_crossref_client(email: str = "noreply@aletheia-probe.org"):
    """Return a LocalCrossrefAdapter when CROSSREF_MODE=local, else the HTTP client."""
    mode = os.environ.get("CROSSREF_MODE", "remote")
    if mode == "local":
        try:
            from aletheia_crossref_adapter import LocalCrossrefAdapter
            return LocalCrossrefAdapter()
        except ImportError as exc:
            raise ImportError(
                "CROSSREF_MODE=local requires the aletheia-crossref-adapter package.\n"
                "Install it from aletheia-probe-crossref-platform/adapter/"
            ) from exc
    return _CrossrefHttpClient(email=email)
```

Where `_CrossrefHttpClient` is a thin wrapper around the existing
`_get_journal_by_issn` HTTP logic, extracted into its own async context
manager class so both paths share the same duck-type interface:

```python
class _CrossrefHttpClient:
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass
    async def get_journal_by_issn(self, issn: str) -> dict | None:
        # existing HTTP logic currently in _get_journal_by_issn
```

Replace the existing `_get_journal_by_issn` call sites in
`CrossrefAnalyzerBackend._search_by_issn` with:

```python
async def _search_by_issn(self, issn: str) -> dict | None:
    async with create_crossref_client(email=self.email) as client:
        return await client.get_journal_by_issn(issn)
```

### 2. No other changes needed in `aletheia-probe`

The `crossref_analyzer` backend only queries by ISSN — name-based search
already returns `None` unconditionally.  The local adapter mirrors that
behaviour (ISSN lookup only).

---

## Environment variables summary

| Variable | Default | Purpose |
|----------|---------|---------|
| `CROSSREF_MODE` | `remote` | Set to `local` to use PostgreSQL adapter |
| `CROSSREF_PG_POOL_MAX` | `10` | Max PostgreSQL connections in pool |
| `CROSSREF_PGHOST` | `localhost` | PostgreSQL host |
| `CROSSREF_PGPORT` | `5432` | PostgreSQL port |
| `CROSSREF_PGUSER` | `crossref` | PostgreSQL user |
| `CROSSREF_PGPASSWORD` | `` | PostgreSQL password |
| `CROSSREF_PGDATABASE` | `crossref` | PostgreSQL database name |
| `CROSSREF_LOADER_EMAIL` | *(required)* | Polite-pool email for loader |

---

## Python version and dependencies

- Python **3.12+** required (same as the existing platform repos).
- `provider/` dependencies: `psycopg2-binary`
- `adapter/` dependencies: `aletheia-crossref-provider` (the provider package above)
- `loader/` dependencies: `aiohttp`, `psycopg2-binary`, `click`

Use `pyproject.toml` with `[project]` table (no `setup.py`).  Follow the
exact same `pyproject.toml` structure as `aletheia-probe-openalex-platform`.

---

## Testing

Provide a minimal test suite under `adapter/tests/` and `provider/tests/`:

- `test_local_provider.py`: given a real or mock psycopg2 connection returning
  a known row, assert that `get_journal_by_issn` returns the correctly
  reconstructed dict with all nested keys.
- `test_adapter.py`: mock `LocalCrossrefProvider`; assert that `__aenter__`
  acquires the semaphore, `get_journal_by_issn` dispatches to provider, and
  `__aexit__` calls `provider.close()` synchronously and releases the semaphore.
- `test_schema.py` (loader): given a sample Crossref API response dict, assert
  that the field mapping produces the expected DB row values.

---

## Deployment checklist

1. Run `crossref-load` once (takes ~1-2 hours at polite-pool rate).
2. Verify row count: `SELECT COUNT(*) FROM crossref_journal` should be ~50 000.
3. Verify ISSN index: `SELECT COUNT(*) FROM crossref_journal_issn` should be ~100 000.
4. Install the adapter package: `pip install -e aletheia-probe-crossref-platform/adapter/`
5. Set `CROSSREF_MODE=local` and the `CROSSREF_PG*` env vars.
6. Restart the `aletheia-probe` process.
7. Confirm no more `Crossref API error: Rate limit exceeded` in the logs.
8. Schedule `crossref-load` monthly via cron/Kubernetes CronJob to refresh data.
