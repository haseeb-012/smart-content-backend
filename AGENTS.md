# Rules for coding agents — smart-content-backend

- Work one approved milestone at a time (see docs/STATE.md → "Next").
- Source of truth order: docs/source/PRD.md → ARCHITECTURE_DECISION.md → REA research → MVP_TECH.md. Log resolved conflicts in docs/DECISIONS.md.
- Python 3.12 + FastAPI, versioned `/api/v1`, modular monolith. No microservices in v1.
- Routers stay thin; logic lives in `app/modules/<module>/`.
- Every protected route depends on `current_profile`. Every workspace read/write goes through `identity.get_workspace_for` (tenant isolation; unknown and foreign workspaces both return 404).
- Errors use `app.core.errors` classes → `{"error": {"code", "message", "details"}}`. Plan limits → `plan_limit` (403); no credits → `credits_exhausted` (402).
- Chargeable actions: `ensure_available()` before work, `consume(idempotency_key=...)` only after success.
- Every provider (billing, scraping, transcription, LLM) sits behind an interface. AI provider is Gemini (D11).
- Stripe is test mode only; never accept live keys; never build card fields.
- Secrets only in `.env` / host env. Never log tokens or keys.
- Update docs/API_CONTRACTS.md and docs/STATE.md when the API changes. Quality gates must pass (see README).
