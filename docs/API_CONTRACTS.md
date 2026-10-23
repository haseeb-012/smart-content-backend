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
| GET | `/collections?category=&q=&sort=most_videos\|name\|recent` | — (free) | → `CollectionSummary[]` `{id, slug, name, category, sub, description, cover_hue, video_count, is_published, updated_at}` — published only. `category`: `formats\|editing_styles\|visual_hooks\|signature_series` |
| GET | `/collections/{slug}` | — (free) | → `CollectionDetail` = summary + `breakdown[]` + `videos[]` `{id, position, platform, url, title, creator_handle, views, outlier_score, transcript, notes}`. 404 for drafts |
| GET | `/collections/{slug}/export?format=markdown\|json` | — (30/min/IP) | → file download (reference sheet) |
| GET | `/admin/collections` · `/admin/collections/{slug}` | 🔒 admin | includes drafts |
| POST | `/admin/collections` | 🔒 admin | `{slug, name, category, sub?, description?, breakdown[]?, cover_hue?, is_published?}` → 201 detail · `slug_taken` (409) |
| PATCH | `/admin/collections/{id}` | 🔒 admin | any of the above → detail (publish/unpublish with `is_published`) |
| DELETE | `/admin/collections/{id}` | 🔒 admin | → 204 |
| POST | `/admin/collections/{id}/videos` | 🔒 admin | `{platform, url (https, must match platform), title, creator_handle?, views?, outlier_score?, transcript?, notes?, position?}` → 201 detail · `duplicate_video` (409) |
| DELETE | `/admin/collections/{id}/videos/{video_id}` | 🔒 admin | → detail |
| GET | `/workspaces/{id}/channels` | 🔒 member | → `WatchlistItem[]` = Channel + `{outlier_videos (≥5x), added_at}`. Channel: `{id, platform, handle, display_name, avatar_url, followers, total_views, video_count, sync_status: pending\|ok\|error, sync_error, last_synced_at}` |
| POST | `/workspaces/{id}/channels` | 🔒 owner/editor | `{url}` or `{platform, handle}` → 201 `{channel, added:true}` (first sync runs in background) · 200 `{added:false}` if already tracked · `invalid_channel` (422) · `channel_not_found` (404) · `plan_limit` (403, `resource: channels`) |
| DELETE | `/workspaces/{id}/channels/{channel_id}` | 🔒 owner/editor | → 204 |
| POST | `/workspaces/{id}/channels/{channel_id}/refresh` | 🔒 owner/editor | → 202 `{queued}` · `rate_limited` (429) within 10 min of last sync |
| GET | `/workspaces/{id}/channels/suggested` | 🔒 member | → `Channel[]` (most-tracked elsewhere, not in this workspace) |
| GET | `/workspaces/{id}/channels/search?q=&platform=` | 🔒 member (30/min) | → `{platform, handle, display_name, followers, channel_id (null = not stored yet), tracked}[]` |
| GET | `/workspaces/{id}/videos?channel_id=…&platform=&min_outlier=&min_views=&max_views=&min_engagement=&posted_within_days=&q=&sort=outlier\|views\|engagement\|newest&limit=48&offset=0` | 🔒 member | → `{items: FeedVideo[], total, limit, offset}`. FeedVideo: `{id, platform, url, caption, thumbnail_url, posted_at, duration_sec, views, likes, comments, shares, engagement_rate, outlier_score, channel}`. Filters AND together. Free |
| GET | `/workspaces/{id}/videos/{video_id}` | 🔒 member | → `{video: FeedVideo, analysis: Analysis\|null, idea: Idea\|null}`. Visible if the workspace tracks the channel or already analyzed it. Poll while `queued/processing`. Free |
| POST | `/workspaces/{id}/videos/{video_id}/analysis` | 🔒 owner/editor (10/min) | → 202 `Analysis` (queued/running) · 200 if already complete (free). `credits_exhausted` (402) · `rate_limited` (429, daily cap). Analysis: `{id, video_id, status: queued\|processing\|complete\|failed, error, provider, model, summary, transcript: [{t:"MM:SS", text}], hook, hook_category, format, visual, idea, created_at, started_at, completed_at}`. 1 credit, charged only on success |
| GET | `/workspaces/{id}/ideas?shortlisted=` | 🔒 member | → `Idea[]` `{id, video_id, analysis_id, source: analysis\|generated\|manual, title, positioning, format, contrast, personalization, shortlisted, created_at}` |
| PATCH | `/workspaces/{id}/ideas/{idea_id}` | 🔒 owner/editor | `{shortlisted?, title?}` → Idea |
| DELETE | `/workspaces/{id}/ideas/{idea_id}` | 🔒 owner/editor | → 204 |
| GET | `/admin/users/{id}` | 🔒 admin | → `{profile, credits}` |
| PATCH | `/admin/users/{id}/plan` | 🔒 admin | `{plan}` → `{profile, credits}` (audited) |
| POST | `/admin/users/{id}/credits` | 🔒 admin | `{amount, reason}` → `{profile, credits}` (audited) |
| PATCH | `/admin/users/{id}/flags` | 🔒 admin | `{mcp_enabled?, is_admin?}` → `{profile, credits}` (audited) |
| GET | `/admin/audit?limit=100` | 🔒 admin | → `AuditEntry[]` |
| GET | `/admin/waitlist` | 🔒 admin | → `{email, plan, source, created_at}[]` |

Frontends keep a copy of this file and update it when the version changes.
