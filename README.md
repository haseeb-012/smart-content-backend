# Smart Content — backend

FastAPI modular monolith. Versioned REST API at `/api/v1`, Supabase Postgres + Auth, Gemini for AI (later steps).

## Run locally

```bash
uv sync                      # install deps (Python 3.12)
cp .env.example .env         # fill in SUPABASE_URL + DATABASE_URL
uv run alembic upgrade head  # create tables (+ RLS and signup trigger on Supabase)
uv run uvicorn app.main:app --reload --port 8000
```

- API docs: http://localhost:8000/docs
- Health: http://localhost:8000/health

Without `.env` the app uses a local SQLite file, and protected routes return `auth_not_configured`.

## Quality gates (run before every commit; CI runs the same)

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy app && uv run pytest
```

## Make yourself admin (after your first sign-in)

In the Supabase SQL editor: `update public.profiles set is_admin = true where email = 'you@example.com';`

## Layout

```
app/
  main.py               FastAPI app, /health, /api/v1
  api/v1/               thin routers: me, plans, workspaces, credits, waitlist, admin
  core/                 config, errors, Supabase JWT verification, deps, rate limit
  models/               SQLAlchemy models
  schemas/              Pydantic request/response = API contract
  modules/identity/     profiles, workspaces, memberships, tenant isolation
  modules/plans_credits plans, credit ledger, billing adapter (mock; Stripe test in step 2)
migrations/             Alembic
tests/                  pytest (SQLite in-memory + local signing key)
docs/                   API_CONTRACTS, DECISIONS, STATE, research/
```
