# Smart Content — Handover (2026-10-23)

## ▶ Resume here (status at end of 2026-10-23)

**All product features are built and pushed.** 115 backend tests green; live e2e passed for every module.

**Real setup done**
- Supabase: database connected (Session pooler, `postgresql+asyncpg`), migrations at `0008` (24 tables), sample Collections (8) + Hooks (16) seeded.
- Supabase website: Site URL `http://localhost:3001`, redirect `…/auth/callback`, Email on, OAuth Server on (`/oauth/consent`, dynamic registration), ES256 signing keys.
- Owner account `haseebsajjad666@gmail.com` signed up and confirmed; `is_admin = true`, `mcp_enabled = true`; workspace "Fitness".
- Stripe test: secret + publishable test keys and all 6 `STRIPE_PRICE_*` IDs filled in.
- Apify token filled in.

**Still to do (in this order)**
1. `GEMINI_API_KEY` → then `LLM_PROVIDER=gemini` (backend `.env`).
2. `CHANNEL_PROVIDER=apify` (token already present).
3. Full test with real Gemini + Apify (verify Apify field mapping, Gemini Interactions response shape, bio-code check fields).
4. Deploy: backend (Render/Railway), dashboard + landing (Vercel). Then set `PUBLIC_API_URL`, `DASHBOARD_URL`, `CORS_ORIGINS`, `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_DASHBOARD_URL`, `NEXT_PUBLIC_MCP_URL`; Supabase Site URL + redirect URL → real domains.
5. Stripe webhook (needs the online URL): Developers → Webhooks → `https://<backend>/api/v1/billing/webhook`, events `checkout.session.completed`, `customer.subscription.created|updated|deleted`, `invoice.paid`, `invoice.payment_failed` → `STRIPE_WEBHOOK_SECRET=whsec_…` → `BILLING_PROVIDER=stripe_test` (the app refuses to start in stripe_test without the secret). Stripe Customer Portal: enable cancel / update payment / switch plans (all 3 products).
6. Test payment with card 4242 4242 4242 4242; connect Claude to `https://<backend>/mcp`.

**Run locally**
- Backend: `cd smart-content-backend && uv run uvicorn app.main:app --port 8000` (uses `.env`; now the real Supabase DB).
- Dashboard: `cd smart-content-dashboard && npm run dev -- -p 3001` (live mode — `.env.local` has Supabase + API URL).
- Landing: `cd smart-content-landing && npm run dev` → :3000.

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
| backend `.env` | `LLM_PROVIDER=gemini`, `GEMINI_API_KEY` | Google AI Studio |
| backend `.env` | `CHANNEL_PROVIDER=apify`, `APIFY_TOKEN` | Apify → Settings → Integrations |
| backend `.env` | `PUBLIC_API_URL` | Public backend URL (MCP lives at `/mcp`) |
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

- Phase 5 steps 3–6 (all live end-to-end, each e2e-tested): **Deep Analysis** (Gemini, 1 credit on success), **Ideas** (+ free generator), **Hooks** (library, generator, watchlist), **Scripts** (versions, free expand, export), **Projects**, **Exports**, **Social verification** (bio code) + **Dashboard analytics**, **Describe your niche** search, **MCP server** (Streamable HTTP + Supabase OAuth, 11 tools). 115 backend tests.

**Runs on demo providers until keys are added:** `LLM_PROVIDER=fake` (set `gemini` + `GEMINI_API_KEY`), `CHANNEL_PROVIDER=fake` (set `apify` + `APIFY_TOKEN`), `BILLING_PROVIDER=mock` (set `stripe_test` + keys). Collections and the hook library hold SAMPLE entries (`scripts.seed_collections`, `scripts.seed_hooks`). Gemini and Apify request/response shapes must be checked on the first real call.

**Next:** final full test with real keys → deploy (backend, dashboard, landing) → set `PUBLIC_API_URL`/`DASHBOARD_URL` to the real HTTPS URLs.

**MCP setup (Supabase):** Authentication → OAuth Server → enable; authorization path `/oauth/consent`; allow dynamic client registration. Then `update public.profiles set mcp_enabled = true where email = '…';`

**Local env files** (gitignored, already created on the original machine — recreate from `.env.example` elsewhere): backend `.env`, dashboard `.env.local`, landing `.env.local`.

**Read first:** backend `docs/DECISIONS.md` (D1–D26), `docs/STATE.md`, `docs/API_CONTRACTS.md`; each frontend's `docs/STATE.md`.

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
