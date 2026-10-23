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

## Next — Phase 5 step 2: Channels + Apify ingestion + outlier score
`ChannelProvider` interface + Apify adapters (TikTok/Instagram/YouTube), workspace watchlist API (plan limit 150/250/1000), `videos` table, background refresh job, outlier score = views ÷ median of the channel's previous 30 shorts (≥10 history). Needs `APIFY_TOKEN`.
