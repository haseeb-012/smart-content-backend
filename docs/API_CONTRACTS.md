# API contract — v1 (backend step 1)

Base URL: `https://api.<domain>/api/v1` (local: `http://localhost:8000/api/v1`). Live schema: `/docs`.
Auth: `Authorization: Bearer <Supabase access token>` on every route marked 🔒.

## Errors
All errors: `{"error": {"code": string, "message": string, "details": object}}`

| HTTP | code | When |
|---|---|---|
| 401 | `unauthorized` / `token_expired` / `auth_not_configured` | Missing, invalid or expired token |
| 403 | `forbidden` | Not allowed (e.g. guest write, non-admin) |
| 403 | `plan_limit` | Plan limit hit. `details: {limit, plan, resource}` |
| 402 | `credits_exhausted` | No credits. `details: {remaining, required, resets_on}` |
| 404 | `not_found` | Missing — or belongs to another user (no existence leak) |
| 422 | `validation_error` | Bad body. `details.errors[]` |
| 429 | `rate_limited` | Too many requests. `details.retry_after` |

## Endpoints

| Method | Path | Auth | Body → Response |
|---|---|---|---|
| GET | `/health` (no prefix) | — | → `{status, version, database}` |
| GET | `/plans` | — | → `Plan[]` `{id, name, credits, workspaces, competitors_per_workspace, verified_channels, refresh_hours, features[], monthly_usd, yearly_usd}` |
| GET | `/me` | 🔒 | → `{profile, plan, credits, billing: {provider, checkout_available, test_mode, subscription_status, billing_cycle, has_billing_account}}` (creates profile on first call) |
| PATCH | `/me` | 🔒 | `{full_name}` → same as GET. Email can't be changed (FR-AUTH-06) |
| GET | `/workspaces` | 🔒 | → `Workspace[]` `{id, name, is_active, role, created_at}` |
| POST | `/workspaces` | 🔒 | `{name}` → 201 `Workspace` · `plan_limit` at plan cap |
| GET | `/workspaces/{id}` | 🔒 | → `Workspace` · 404 if not a member |
| PATCH | `/workspaces/{id}` | 🔒 | `{name?, is_active?}` → `Workspace` (owner only for is_active) |
| GET | `/credits` | 🔒 | → `{allowance, used, adjustments, remaining, period_start, resets_on}` |
| GET | `/credits/history?limit=50` | 🔒 | → `{credits, entries: [{id, kind, amount, action, workspace_id, created_at}]}` |
| POST | `/waitlist` | — (5/min/IP) | `{email, plan, source?}` → `{ok, already_joined}` |
| POST | `/billing/checkout-session` | 🔒 | `{plan, cycle: monthly\|yearly}` → `{url}` (Stripe-hosted Checkout, test mode). Errors: `billing_unavailable` (409, mock or price not set), `already_subscribed` (409 → use portal) |
| POST | `/billing/portal-session` | 🔒 | → `{url}` (Stripe Customer Portal). `no_billing_account` (409) if never checked out |
| POST | `/billing/webhook` | Stripe signature | raw body + `Stripe-Signature` → `{received, duplicate}`. `invalid_signature` / `live_event_rejected` (400) |
| GET | `/admin/users/{id}` | 🔒 admin | → `{profile, credits}` |
| PATCH | `/admin/users/{id}/plan` | 🔒 admin | `{plan}` → `{profile, credits}` (audited) |
| POST | `/admin/users/{id}/credits` | 🔒 admin | `{amount, reason}` → `{profile, credits}` (audited) |
| PATCH | `/admin/users/{id}/flags` | 🔒 admin | `{mcp_enabled?, is_admin?}` → `{profile, credits}` (audited) |
| GET | `/admin/audit?limit=100` | 🔒 admin | → `AuditEntry[]` |
| GET | `/admin/waitlist` | 🔒 admin | → `{email, plan, source, created_at}[]` |

Frontends keep a copy of this file and update it when the version changes.
