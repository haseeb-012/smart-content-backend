# Backend — current state (2026-10-27)

## ▶ Resume here (status at end of 2026-10-27)

**Live and deployed.** 131 backend tests green.
- Backend: Render — `https://smart-content-backend-k2qh.onrender.com` (free plan: sleeps after 15 min, ~50 s wake-up).
- App: `https://smart-content-dashboard.vercel.app` · Website: `https://smart-content-landing.vercel.app` (links to the app fixed).
- Real services on: Supabase DB + Auth, Gemini `gemini-3.8-flash`, Apify (free plan, **$5/month — $3.47 left on 2026-10-27**, resets 2026-11-10).

**2026-10-27**
- Full live feature test: 31/31 passed (TikTok/YouTube/Instagram import, Deep Analysis on all 3 incl. Instagram, scripts, hooks, ideas, niche search, bio verification, MCP, credits) + Stripe test checkout for all 6 prices + portal.
- Stripe: Render has `BILLING_PROVIDER=stripe_test` + webhook secret. A signed test event to the live webhook returned 200 (test row removed). Stripe destination "Smart_content" is Active with 6 events. **Not yet done: one real test payment (card 4242…) to see a subscription saved.**
- Apify free plan has a 16 GB memory cap: niche search launches ~6 TikTok lookups at 4 GB each, so some get refused (402). Owner chose not to change it for now. Fix if needed: cap concurrent Apify runs at 3 and request 2 GB per run.
- **Admin panel built** (dashboard `/admin`, see "Done — Admin panel" below). Login is defined in env: `ADMIN_EMAIL`, `ADMIN_PASSWORD_HASH`, `ADMIN_SESSION_SECRET` (local `.env` + `deploy-env/render-backend.env` filled; **must be added on Render**).
- Collections still hold the 8 seeded **sample** collections (fake video links). Replace them in Admin → Collections ("Remove sample videos" + "Pick from your videos"), or hide them.
- Refresh-cost change (import 100 once / manual refresh 10 newest / plan-based verified refresh) is **proposed only — do not apply without the owner's go**.

- **Thumbnails kept permanently** (migration `0009`, applied to Supabase on 2026-10-27): TikTok/Instagram image links expire after ~2–5 days, so after every import the backend saves a small WebP copy (~15 KB) in `video_thumbnails` and the API returns `{PUBLIC_API_URL}/api/v1/thumbnails/{video_id}`. YouTube links don't expire and are used as-is. Back-filled 115 copies; khaby.lame re-imported (its links had expired) → 60 copies. `scripts/backfill_thumbnails.py` copies any still-valid links. DB cost ≈ 1 MB per 60-video channel — move to Supabase Storage if it grows.
- App fixes: a creator colour now shows behind any thumbnail that fails (never a blank box); popups (e.g. Create workspace) render above the whole page instead of inside the sidebar, and focus the first field.
- Website screenshots (`smart-content-landing/public/screens`) re-taken from the owner's real data (videos, analysis, channels, ideas, hooks, script, collections). `dashboard.png` kept until the owner's own account has real stats.

**Next**
1. Add the 3 `ADMIN_*` values on Render → Save; then sign in at `https://smart-content-dashboard.vercel.app/admin/login`. Change the temporary password to a strong one (`uv run python scripts/admin_password.py`).
2. One real Stripe test payment; confirm the plan and the Stripe events in Admin → Billing.
3. Replace sample collections with real outliers.
4. Rotate the secrets that were pasted in chat (DB password, Gemini key, Apify token, Stripe key) and update Render.
5. Watch Apify credit in Admin → System & costs.

**Run locally**
- Backend: `cd smart-content-backend && uv run uvicorn app.main:app --port 8000` (uses `.env`; the real Supabase DB).
- Dashboard: `cd smart-content-dashboard && npm run dev -- -p 3001` (live mode — `.env.local` has Supabase + API URL).
- Landing: `cd smart-content-landing && npm run dev` → :3000.

## Done — Admin panel (2026-10-27)
- Dashboard `/admin` (own sign-in at `/admin/login`, hidden from search engines). Pages: Overview (counts + 30-day charts), System & costs (services, which keys are set — yes/no only, Apify spend, failed imports, stuck analyses), Users (search/filter, CSV) + user detail (plan, credits +/- with reason, MCP and admin switches, usage, credit history, social accounts), Billing (Stripe setup checks, recent events, subscriptions), Waitlist (search, CSV), Collections (create, edit, cover, publish/hide, delete, remove sample videos, add videos from real outliers with transcript + analysis, or by link), Hook library (add/edit/hide/delete), Channels (status, errors, re-import), Analyses (status filter, errors), Audit log (filter).
- Access: the env admin login (PBKDF2 hash, 8 h HS256 token with its own audience — rejected on normal app routes; 5 tries/15 min per IP + 30 failures/h overall; changing `ADMIN_EMAIL` or the secret signs everyone out), **or** a Supabase account with `is_admin` (they also see an "Admin panel" link in the app sidebar). The env admin has no Supabase account; its audit rows show "Admin (panel login)".
- New API: `POST /admin/login`, `GET /admin/session|stats|system|billing|users|users/{id}/usage|channels|analyses|videos|hooks`, `POST /admin/channels/{id}/resync`, `PATCH /admin/hooks/{id}`; duplicate hook → 409 `duplicate_hook`.
- Tested: 12 new backend tests; admin API against the real DB 16/16; browser test of every page 21/21 (login, wrong password, all pages, user detail, collection editor + picker, phone width, sign out, no page errors).

## Earlier resume notes (2026-10-23, kept for history)

**All product features are built and pushed.** 116 backend tests green; live e2e passed for every module.

**Real setup done**
- Supabase: database connected (Session pooler, `postgresql+asyncpg`), migrations at `0008` (24 tables), sample Collections (8) + Hooks (16) seeded.
- Supabase website: Site URL `http://localhost:3001`, redirect `…/auth/callback`, Email on, OAuth Server on (`/oauth/consent`, dynamic registration), ES256 signing keys.
- Owner account `haseebsajjad666@gmail.com` signed up and confirmed; `is_admin = true`, `mcp_enabled = true`; workspace "Fitness".
- Stripe test: secret + publishable test keys and all 6 `STRIPE_PRICE_*` IDs filled in.
- Apify token filled in.

**Late 2026-10-23 updates**
- Owner switched `CHANNEL_PROVIDER=apify` in the local `.env`. Apify token works (Apify answered `201 Created`).
- GitHub CI failure fixed (`3ef136a`): an MCP test polled past the 120/min MCP rate limit on slow CI. Tests now:
  - never use real services: `tests/conftest.py` forces demo providers and empty keys, whatever the local `.env` says
  - wait for background imports instead of polling
  - keep a separate test proving the rate limit still works

  Local result: 116 passed (one unexplained single failure in ~13 runs; check CI).
- Dropdowns/inputs ignoring a custom width fixed (dashboard `bd002fe`). The Connectors tool list now shows correctly.

**Still to do (in this order) — START HERE TOMORROW**
1. **Live Apify test** with one real creator (owner picks a handle): start backend → add the creator via the API → check that real videos, views and outlier scores come back. This verifies the Apify field mapping in `providers.py` (still unverified) — fix the mappers if fields differ.
2. `GEMINI_API_KEY` → then `LLM_PROVIDER=gemini` (backend `.env`).
3. Full test with real Gemini + Apify: analysis on a real TikTok/Instagram/YouTube video (verifies the Gemini Interactions response shape and the media download), scripts, ideas, hooks, niche search, bio-code verification fields.
4. Deploy: backend (Render/Railway), dashboard + landing (Vercel). Then set `PUBLIC_API_URL`, `DASHBOARD_URL`, `CORS_ORIGINS`, `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_DASHBOARD_URL`, `NEXT_PUBLIC_MCP_URL`; Supabase Site URL + redirect URL → real domains.
5. Stripe webhook (needs the online URL): Developers → Webhooks → `https://<backend>/api/v1/billing/webhook`, events `checkout.session.completed`, `customer.subscription.created|updated|deleted`, `invoice.paid`, `invoice.payment_failed` → `STRIPE_WEBHOOK_SECRET=whsec_…` → `BILLING_PROVIDER=stripe_test` (the app refuses to start in stripe_test without the secret). Stripe Customer Portal: enable cancel / update payment / switch plans (all 3 products).
6. Test payment with card 4242 4242 4242 4242; connect Claude to `https://<backend>/mcp`.

**How MCP works (for the owner)**: Claude → Settings → Connectors → Add custom connector → paste `https://<backend>/mcp` → browser opens → sign in → `/oauth/consent` → Allow access. Then ask in plain language; 11 tools, Allow/Block per tool in Settings → Connectors. Only possible after deployment (Claude needs a public HTTPS URL).

**Run locally**
- Backend: `cd smart-content-backend && uv run uvicorn app.main:app --port 8000` (uses `.env`; now the real Supabase DB).
- Dashboard: `cd smart-content-dashboard && npm run dev -- -p 3001` (live mode — `.env.local` has Supabase + API URL).
- Landing: `cd smart-content-landing && npm run dev` → :3000.

## Done — step 1: foundation
- FastAPI app, `/health`, `/api/v1`, CORS, structured errors.
- Supabase access-token verification via JWKS (signature, expiry, audience, issuer).
- Migration `0001`: profiles, workspaces, memberships, credit_ledger, audit_log, waitlist (+ RLS, signup trigger on Supabase).
- Workspaces with plan limits and tenant isolation; plans read from `plan` attribute.
- Credit ledger: idempotent, charge-after-success, anniversary reset, no rollover.
- Admin: plan, credits, flags, audit, waitlist. Public waitlist with rate limit.

## Done — step 2: Stripe test billing
- `StripeTestBillingAdapter` behind `BillingAdapter` (`BILLING_PROVIDER=stripe_test`), `MockBillingAdapter` default.
- `POST /billing/checkout-session` → Stripe-hosted Checkout (subscription mode, dynamic payment methods, customer created once, double-click idempotency key).
- `POST /billing/portal-session` → Customer Portal.
- `POST /billing/webhook`: signature verified with Stripe's SDK, live-mode events rejected, event-ID idempotency (`stripe_events` table, migration `0002`).
  Handles `checkout.session.completed`, `customer.subscription.created/updated/deleted`, `invoice.paid`, `invoice.payment_failed`.
  Price → plan mapping from env; credits follow the subscription period start; cancellation → default plan; `incomplete` never grants a plan; stale subscriptions can't cancel the current one. Plan changes audited.
- Startup refuses live keys and refuses `stripe_test` without a test key + webhook secret.
- 36 tests passing; ruff + mypy clean.

## Not verified yet
- Against real Supabase and a real Stripe test account (needs keys in `.env`).

## How to test with Stripe (test mode)
1. Stripe Dashboard (Test mode) → create products Pro / Visionary / Titan with monthly + yearly recurring prices → copy Price IDs to `.env`.
2. Create a restricted key (`rk_test_…`) → `STRIPE_SECRET_KEY`.
3. `stripe listen --forward-to localhost:8000/api/v1/billing/webhook` → copy `whsec_…` → `STRIPE_WEBHOOK_SECRET`; set `BILLING_PROVIDER=stripe_test`.
4. `uv run alembic upgrade head`, run the API, call checkout, pay with card 4242 4242 4242 4242.
5. Enable the Customer Portal in Stripe (Settings → Billing → Customer portal), allowing plan switches between the three products.

## Done — step 3: dashboard connected
`/me` now also returns `subscription_status`, `billing_cycle`, `has_billing_account`. Dashboard uses Supabase Auth + this API in live mode (see dashboard docs/STATE.md). E2E-verified with a local JWKS stand-in.

## Done — Phase 5 step 1: Collections
- Tables `collections` + `collection_videos` (migration `0003`, RLS on Postgres).
- Public, free reads (no auth, no credits): list with category/search/sort, detail, Markdown/JSON reference-sheet export (rate-limited).
- Admin curation API (create/update/publish/delete, add/remove example videos with platform-checked https URLs), every write audited.
- `uv run python -m scripts.seed_collections` seeds 8 clearly-labelled SAMPLE collections for dev/staging.
- Dashboard Collections pages use the API in live mode (verified e2e: 6/6).
- 41 tests; ruff + mypy clean.

## Done — Phase 5 step 2: Channels + ingestion + outlier score + Discover feed
- Tables `channels` (global, one fetch per creator), `workspace_channels`, `videos` (migration `0004`).
- `ChannelProvider` interface: `FakeChannelProvider` (default, deterministic demo data) and `ApifyChannelProvider`
  (TikTok `clockworks~tiktok-scraper`, Instagram `apify~instagram-scraper`, YouTube `streamers~youtube-shorts-scraper`; IDs configurable).
  ⚠ Apify field mapping is written from the actors' docs and unit-tested with assumed shapes — **verify on the first real run** and adjust `providers.py` mappers if fields differ.
- Watchlist API: add by URL or handle (validates the account exists), remove, manual refresh (10-min cooldown), plan limit per workspace.
- Ingestion in a background task: upsert videos, engagement rate, outlier score = views ÷ median(previous 30), needs ≥10 history. Errors are recorded on the channel, never crash the request.
- Daily refresh: `uv run python -m scripts.refresh_channels [--hours 24]` (schedule with host cron).
- Discover feed with all PRD filters (AND), 4 sorts, paging. Search (known channels + exact-handle lookup) and suggestions.
- Never transcribes or AI-analyses scraped videos (only on Deep Analyze — next step).
- 62 tests; ruff + mypy clean. Dashboard Channels + Videos live (e2e 9/9).

## Done — Phase 5 step 3: Deep Analysis
- Tables `video_analyses` (one per workspace + video) and `ideas` (migration `0005`, RLS on Postgres).
- `LLMProvider` interface (`app/modules/discovery_analysis/llm.py`): `FakeLLMProvider` (default) and `GeminiLLMProvider`
  (Interactions API, model `gemini-3.8-flash`, JSON-schema output). ⚠ Verify the request/response shape on the first real call.
- Flow: POST analysis → queued (balance checked, daily cap) → background run → complete + idea created + **1 credit charged in the same commit**. Failure → `failed` with a clear message, nothing charged, retry allowed. Re-open is free.
- Media: YouTube by URL; TikTok/Instagram get a fresh direct link for that one video via Apify, downloaded in memory under strict safety rules (D22), never stored.
- Ideas API: list (filter shortlisted), shortlist/rename, delete.
- 81 tests; ruff + mypy clean. Dashboard video page live (cards → analysis screen with polling).

## Done — Create module (Ideas generator, Hooks, Scripts, Projects, Exports)
- Tables `hook_library`, `saved_hooks`, `scripts` + `script_versions`, `projects` + `project_items`, `exports` (migration `0006`).
- `LLMProvider` gained `write_script`, `expand_script`, `generate_hooks`, `generate_ideas`, `suggest_creators` (fake + Gemini).
- Scripts: 1 credit on success, idempotent via `Idempotency-Key`; edit → version; expand free; restore; export md/json/txt.
- `uv run python -m scripts.seed_hooks` seeds 16 SAMPLE hooks.
- 97 tests; ruff + mypy clean.

## Done — Social verification, own-account analytics, niche search
- Tables `social_accounts`, `channel_stats` (migration `0007`). `/me/social` (start, verify, remove), `/me/analytics?days=7|30|90`.
- `POST /workspaces/{id}/channels/niche-search` — AI suggestions verified against the platform.
- 108 tests; ruff + mypy clean. Dashboard + Settings → Social verification + Channels niche tab live (e2e 11/11).

## Done — MCP server
- `POST /mcp` (Streamable HTTP, stateless) + OAuth protected-resource metadata; Supabase Auth OAuth 2.1 server is the authorization server.
- 11 tools over the existing services (tenant checks, plan limits, credits unchanged). Per-user tool Allow/Block (`/me/mcp`, migration `0008`).
- Dashboard: `/oauth/consent` (approve/deny via supabase-js `auth.oauth`), Settings → Connectors live.
- 115 tests; official MCP Python client check 9/9.
- To turn on for real: Supabase → Authentication → OAuth Server: enable, authorization path `/oauth/consent` (Site URL = dashboard), allow dynamic client registration; set `PUBLIC_API_URL` to the public backend URL (HTTPS in production); `update profiles set mcp_enabled = true where email = '…'`.

## Next
See **Resume here** at the top.
