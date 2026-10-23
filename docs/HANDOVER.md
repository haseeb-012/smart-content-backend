# Smart Content — Handover (2026-10-23)

Short-form video research & scriptwriting SaaS (layout modelled on sandcastles.ai, own brand).
Three repos, one product:

| Repo | What | Stack |
|---|---|---|
| https://github.com/haseeb-012/smart-content-landing | Marketing site (home, pricing, FAQ, /mcp, /collections, legal) | Next.js 16, Tailwind 4 |
| https://github.com/haseeb-012/smart-content-dashboard | The app ("studio") | Next.js 16, Tailwind 4, Supabase Auth (`@supabase/ssr`) |
| https://github.com/haseeb-012/smart-content-backend | API — source of truth | Python 3.12, FastAPI, SQLAlchemy + Alembic, Supabase Postgres, Stripe test mode |

Put all three side by side in one folder (e.g. `smartcontent-mvp/`).

---

## 1. Set up on a new machine (10 minutes)

Needs: Node 22+, Python 3.12, [uv](https://docs.astral.sh/uv/), Git, GitHub CLI (`gh`).

```bash
mkdir smartcontent-mvp && cd smartcontent-mvp
gh repo clone haseeb-012/smart-content-landing
gh repo clone haseeb-012/smart-content-dashboard
gh repo clone haseeb-012/smart-content-backend
```

Commit as the project identity **only inside these repos** (doesn't change your global git):
```bash
for r in smart-content-*; do git -C $r config user.name "Haseeb Sajjad"; git -C $r config user.email "haseebsajjad666@gmail.com"; done
```

### Run it
```bash
# Backend  → http://localhost:8000/docs
cd smart-content-backend && uv sync && cp .env.example .env   # fill in values (section 2)
uv run alembic upgrade head && uv run uvicorn app.main:app --reload --port 8000

# Dashboard → http://localhost:3001
cd smart-content-dashboard && npm install && cp .env.example .env.local
npm run dev -- -p 3001

# Landing → http://localhost:3000
cd smart-content-landing && npm install && npm run dev
```
Without keys the dashboard runs in **Demo mode** (local data, amber badge) — everything is clickable.

### Quality checks (run before every commit)
- Backend: `uv run ruff format --check . && uv run ruff check . && uv run mypy app && uv run pytest`
- Frontends: `npx tsc --noEmit && npm run lint && npm run build`

---

## 2. Keys needed to go "live" (none are in git — never commit them)

| Where | Variable | From |
|---|---|---|
| backend `.env` | `SUPABASE_URL`, `DATABASE_URL` | Supabase → Project Settings (use `postgresql+asyncpg://…`) |
| backend `.env` | `BILLING_PROVIDER=stripe_test`, `STRIPE_SECRET_KEY` (rk_test_…), `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_*` (6) | Stripe **test mode** (see backend `docs/STATE.md`) |
| backend `.env` | `GEMINI_API_KEY` | Google AI Studio (used from Phase 5) |
| dashboard `.env.local` | `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`, `NEXT_PUBLIC_API_URL` | Supabase → API; backend URL |

Supabase: add `<dashboard-url>/auth/callback` to Auth → Redirect URLs; enable Email (+ Google/Apple if wanted).
Make yourself admin after first login: `update public.profiles set is_admin = true where email = '…';`

---

## 3. Where things stand

**Done**
- Landing: all pages, dark navy + electric-blue palette, own logo (3 rising bars + dot), real dashboard screenshots.
- Dashboard: every PRD screen (Dashboard, Videos, Analysis, Channels, Ideas, Hooks, Collections, Scripts, Projects, Exports, Settings ×6, Auth, Onboarding). Live mode wired to Supabase + API for login, workspaces, credits, profile, Stripe test checkout/portal — verified end-to-end (8/8).
- Backend step 1 (auth, workspaces, plans, credit ledger, admin, audit, waitlist) + step 2 (Stripe test billing). 36 tests.

- Phase 5 step 1: **Collections** live end-to-end (public free API, admin curation, export, dashboard wired). Seed samples with `uv run python -m scripts.seed_collections`.

- Phase 5 step 2: **Channels watchlist + video ingestion + outlier score + Discover feed** live end-to-end (fake provider by default; Apify adapter ready — set `CHANNEL_PROVIDER=apify` + `APIFY_TOKEN`, then verify field mapping on the first run).

**Not real yet (still demo data):** AI analysis/ideas/scripts (Gemini), hooks library, dashboard analytics, social verification, MCP server. Collections hold SAMPLE entries until the team curates real ones. Real creator data needs an Apify token.

**Next (in order):** Phase 5 step 3: Deep Analysis (Gemini) → Ideas/Hooks/Scripts/Projects/Exports → MCP.

**Local env files** (gitignored, already created on the original machine — recreate from `.env.example` elsewhere): backend `.env`, dashboard `.env.local`, landing `.env.local`.

**Read first:** backend `docs/DECISIONS.md` (D1–D15), `docs/STATE.md`, `docs/API_CONTRACTS.md`; each frontend's `docs/STATE.md`.

**Rules that must not break:** Stripe test mode only (app refuses live keys) · browser never gets secret keys · every provider behind an interface · AI = Gemini · never copy Sandcastles' logo/text/images · one milestone at a time, tests green before moving on.

---

## 4. Prompt to continue (paste into Claude Code / any coding agent, opened in the `smartcontent-mvp` folder)

```
You are continuing the "Smart Content" project: a short-form video research and
scriptwriting SaaS (layout modelled on sandcastles.ai, our own brand).

The folder has 3 repos: smart-content-landing (Next.js 16 marketing site),
smart-content-dashboard (Next.js 16 app, Supabase Auth, demo + live modes) and
smart-content-backend (FastAPI /api/v1, Supabase Postgres, Stripe TEST mode).

Before doing anything, read:
- smart-content-backend/docs/HANDOVER.md
- smart-content-backend/docs/DECISIONS.md, docs/STATE.md, docs/API_CONTRACTS.md
- smart-content-dashboard/docs/STATE.md and smart-content-landing/docs/STATE.md
- each repo's AGENTS.md (Next.js 16 has breaking changes — read node_modules/next/dist/docs before writing Next code)

Rules: work one milestone at a time; keep all quality checks green (see HANDOVER §1);
Stripe test mode only; no secrets in git or in the browser; every provider behind an
interface; AI provider is Gemini; never copy Sandcastles' logo, text or images;
log new decisions in backend docs/DECISIONS.md and update the STATE.md files.
Commit inside each repo with the repo-local identity (Haseeb Sajjad
<haseebsajjad666@gmail.com>) and push to origin main.

Next milestone: <write what you want, e.g. "Phase 5 step 1: Collections — backend
models + admin curation endpoints + public read API, then wire the dashboard
Collections pages to it">.

First tell me your plan in a few lines, then build it, test it, and summarize.
```
