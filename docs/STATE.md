# Backend — current state (2026-10-23)

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

## Next — Phase 5 step 3: Deep Analysis (Gemini)
Async analysis job (queued → processing → complete), 1 credit charged on success only (idempotent key per workspace+video), Gemini video understanding → transcript, summary, idea, hook, format, visual; idea auto-created; cost cap per user. Needs `GEMINI_API_KEY`. Consider adding Redis + a worker here (master prompt: Celery when analysis starts).
