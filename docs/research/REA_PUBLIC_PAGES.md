# REA findings — sandcastles.ai public pages

Captured 2026-10-23 with `rea-agents@6.7.0` (CLI: `list-browser-targets`, `inspect-web-page`, `capture-web-screenshot`)
against a temporary Chrome 155 profile at 1440px wide. Observe-only: no logins, no forms submitted.
Screenshots stay in the local scratchpad and are NOT committed (they contain Sandcastles brand assets).

## Evidence IDs
| What | Evidence |
|---|---|
| Target list | ev_16df7960f774f28cdd8a8d79c3843019525ce1e92f003453488b6d3f9429ac24 |
| Home page inspect (DOM, a11y, scripts) | ev_ec9fef9d594bd65281798ecfba860182ff59b488034a18b833c72d215f4b8977 |
| Home screenshots (hero → footer) | ev_2403fd65…, ev_2873222e…, ev_578f712f…, ev_d394168b…, ev_8e3fcf5e…, ev_64838ecc…, ev_42c5f9db…, ev_008ac631…, ev_3d80c436…, ev_c43e68b3… |
| /mcp screenshots | ev_333ba461…, ev_71e08eda…, ev_23d99258…, ev_330bb2c0…, ev_4aca5351…, ev_5fb900e4…, ev_307ed42f… |
| /collections screenshots | ev_469eccb4…, ev_ebcf9e1c…, ev_7006bf18…, ev_9fd64a56…, ev_3866b0f9…, ev_20d51c77…, ev_b29e2133… |

## Observed stack (marketing site)
- Vite-built React SPA (`/assets/index-*.js`, route chunks `Home-*.js`, `Layout-*.js`, `Footer-*.js`, `Outliers-*.js`, `Cta-*.js`, `TagPill-*.js`, `RichText-*.js`).
- Tailwind CSS with custom breakpoints named `tab`, `land`, `mob` (from class names).
- Swiper (`swiper.esm-*.js`) for the feature carousel; Vimeo player (`player.es-*.js`) for videos.
- Google Analytics (gtag), Tolt (affiliate) script.
- Self-hosted fonts: Inter (body), Host Grotesk Medium/SemiBold (display).
- Login lives on `signin.sandcastles.ai`; help on `help.sandcastles.ai`.

## Measured design tokens (computed styles, 1409px viewport)
| Token | Value |
|---|---|
| Page background | `rgb(2,8,20)` (#020814) — whole site is dark |
| Body text | Inter 16px / 24px, letter-spacing −0.32px, `rgba(255,255,255,.8)` |
| H1 | Host Grotesk 500, 64px / 64px, −1.28px |
| H2 | Host Grotesk 500, 48px / 57.6px, −0.96px |
| H3 (feature card) | Host Grotesk 500, 20px / 28px, −0.2px |
| H3 (step) | Host Grotesk 500, 24px / 32px, −0.32px |
| Accent | #014CE3 (theirs — we use our own violet) |
| Nav pill | max-width 1042px, bg `rgba(21,34,60,.66)`, 1px border `rgba(255,255,255,.02)`, padding 8 8 8 20, fully rounded, fixed top 16px |
| Nav links | Inter 14px/500, padding 6px 16px, white |
| Nav CTA | bg #15223C, Host Grotesk 14px/600, padding 8px 16px, fully rounded |
| Section container | 1280px max, 64px side gutter |
| Card | bg `rgb(4,14,34)`, radius 16px, 1px border `rgba(255,255,255,.1)`, padding 24px |
| Toggle (Monthly/Yearly) | bg `rgba(255,255,255,.1)`, 1px border white/10, padding 4px, fully rounded |
| Section heights | hero 1301, features 829, how-it-works 912, outliers 798, CTA/testimonials 833, pricing 1178, faq 711, walkthrough 745 |

## Home page section order
1. Fixed pill nav. 2. Hero: two-line headline with accent words, subtitle, CTA, large app preview (sidebar + Videos page).
3. Features: centered H2 + Swiper carousel of 8 cards (image top, H3 with accent words, one-line text), prev/next arrow buttons.
4. How it works: left column H2 + 4 accordion-style steps ("STEP 1" label, H3), right column visual panel.
5. Outliers band: pill label, H2 with accent, floating stat cards (outlier score / views / likes / engagement / followers).
6. Testimonials carousel + CTA band with glowing horizon line.
7. Pricing: pill label, H2, Monthly/Yearly toggle with "save 20%" sticker, 3 cards (middle highlighted with accent border + "Popular" chip), check-list features, "Get Started →" button.
8. FAQ: left big H2 "Still have questions?", right General/Billing/Account tabs + accordion.
9. Walkthrough: card with left headline + right product collage with "Watch walkthrough" button.
10. Footer: logo left, 3 link columns (Product, Solutions, Resources), divider, copyright + legal links, glow.

## App UI glimpsed in marketing previews (input for dashboard UI_SPEC)
- Sidebar groups: **Research** (Videos, Ideas), **Create** (Scripts, Projects), then Settings, Help Center at bottom.
- Videos page: title + subtitle, Feed / Vault segmented tabs, actions "Add video URL", "Sort by", "Export".
- Left filter panel: Saved filters (Show), Filters (Clear), Channels avatar picker, Engagement range, Posted within N Months, Platform select, Keywords, "Save filter".
- Video card: 9:16 thumbnail with platform badge, title, @handle, relative date, three chips — outlier `99.1x` (green), views `12M` (blue), engagement `2%` (orange).
- Analysis stat grid: Outlier score, Views, Likes, Engagement rate, Comments, Channel followers.
- Script view: "Previous version" vs "Current script" diff.
- Watchlist popover: "Add this channel to a watchlist" + select.

## /mcp page
Pill label → H1 (2 lines, second accent) → subtitle → "Get started" + "Connect your assistant" → "Works with" logo row →
problem section (generic vs data-backed answer, two cards) → solution (4 numbered steps) → "Use cases" 3-col grid of prompt cards (category label, quoted prompt) →
connect cards per assistant → FAQ → closing CTA → footer.

## /collections page
Pill label → H1 → subtitle → "Browse Collections" + "See what's inside ↓" → panel card: "Collections" title, search input, Category + Sort by chips, 4-col cards
(gradient cover, CATEGORY · SUB label, name, "N videos" chip) → two-column check list → "100+ collections, four categories" + 4 stat cards →
example tag cloud → "Test out popular collections" → FAQ → footer.

## Pricing → signup redirect chain (observe only, no login)
Evidence: inspect `ev_f4dba9aac1dfc97d5ba3c5ee51de7e045641539d257a6f3cb55b05e180154aca`, session `ev_24719d360f8acb245e2e8ddd89628b932d1af198aa4c63f3490a30ae752c463b`.
- Pricing cards are `<a data-cta="pricing-{plan}" href="#">`; click → script-initiated navigation (no static href).
- Lands on `signin.sandcastles.ai/sign-up?client_id=client_…&redirect_uri=https://app.sandcastles.ai/login&state=…&authorization_session_id=…` — WorkOS AuthKit hosted sign-up.
- `state` carries only analytics/attribution (GA client id, FB pixel id, visitor id, landing variant) — **no plan or price**. Inference: plan choice + Stripe Checkout happen inside the app after signup.
- Our design (D12): landing CTA → `dashboard/signup?plan=&cycle=` → after auth, app calls `POST /api/v1/billing/checkout-session` → Stripe hosted Checkout (test mode).

## Unknowns
- Mobile (390px) layout not yet captured — next REA pass.
- Logged-in app (app.sandcastles.ai) — needs user's HAR files; current dashboard spec is based on marketing previews + PRD §12.
- Network/API shapes: none observed on public pages (static SPA).
