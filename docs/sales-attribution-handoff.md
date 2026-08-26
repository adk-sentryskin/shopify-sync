# Sales Attribution — Handoff & Resume Doc

**Last updated:** 2026-08-24
**Owner:** namit
**Status:** **PCD approved. Pipeline live. Zero attributed orders in 60 days.**

The plumbing is finished and working end to end — merchants have granted `read_orders`, the `orders/*` webhooks are registered and delivering, and 149 real orders came through in the last 60 days. **None of them carried agent attribution**, so `shopify_sync.orders` is empty. That is not a bug in this pipeline; it means nobody has completed a purchase through the widget's add-to-cart. See "2026-08-24 investigation" below — including the one number that distinguishes the three possible causes, which lives in GA4 and not in this repo.


---

## ▶️ Start here tomorrow (2026-08-25)

Cold-start checklist. Ordered so the cheapest thing that changes the decision comes first.

**⚠️ First: there is uncommitted work in the tree.** Nothing is committed or deployed.
`git status` should show 6 modified files + `scripts/verify_product_sync.py` untracked.
If it doesn't, something ate it — see "Uncommitted work in the tree" for what should be
there. Sanity check before touching anything:

```bash
cd ~/Documents/work/chekout-ai/shopify-sync
git status --short                                    # expect 6 M + 1 ??
PYTHONPATH=. python scripts/verify_product_sync.py     # expect: all 49 checks passed
```

**1. Get the GA4 number (~5 min, do this first).**
Query `add_to_cart_clicked` over the last 60 days, broken out by `merchantId` — focus on
`rosendahl-design-group` and `pacsoulfoods` (86 and 60 orders respectively in that window,
zero attributed). Also grab widget session counts. The three-way table in the 2026-08-24
section says what each outcome means. **Everything below is worth less until this is known** —
it decides whether the next sprint is widget UX, checkout funnel, or attribution plumbing.

**2. Redeploy with CPU throttling off (one flag).**
Un-starves every FastAPI background task — OAuth product sync, custom-app sync, order
provisioning, order backfill. This is why the backfill silently timed out.

```bash
gcloud run services update shopify-sync \
  --region=us-central1 --project=production-aibuilder --no-cpu-throttling
```

Confirm: `gcloud run services describe shopify-sync --region=us-central1 \
  --format="value(spec.template.metadata.annotations)"` should now include
`run.googleapis.com/cpu-throttling: 'false'`.

**3. Land the uncommitted work as three commits** (details in "Uncommitted work in the tree"):

| # | Scope | Urgency |
|---|---|---|
| 1 | Product sync: page-batched writes + failsafes | 48–63× speedup, has been slow but not broken |
| 2 | Tenant-filter fixes (9 sites) | **Urgent** — `products/delete` and GDPR `shop/redact` have 500'd since January |
| 3 | Orders: `updated_at` in the field allowlist | Small |

Commit 2 is the one that shouldn't wait. Consider putting it on its own branch off `main`
so it can ship independently of the orders/sync stack (which still isn't merged — see
"branch state"). No `Co-Authored-By` trailer.

Then `echo yes | ./deploy.sh production`.

**4. After 2 + 3 are deployed, re-run the 60-day backfill** for `rosendahl-design-group`
and `nuthatch-naturals` — it has never once succeeded for either.

### Deliberately NOT done yet

- Session-id unification (the broken JOIN key) — blocks Track 3, but pointless until step 1
  says there's a funnel worth joining.
- Surfacing sync health to the frontend. Decided approach: `last_sync_at` /
  `last_sync_status` / `last_sync_stats` (JSONB) columns on `shopify_stores`, written by the
  background task, returned by `GET /api/sync/status`. One migration. Not started.
- `backfill_attributed_orders` pagination (`since_id`).
- Success-path logging in `_handle_order_event`.

---

## TL;DR

We're shipping **sales attribution** — connecting agent-routed cart additions to the orders that actually convert. The chat agent stamps `_chekout_ai_*` attributes on the Shopify cart; the backend reads them off the resulting orders; the dashboard surfaces conversion + revenue per merchant.

The build is **phased** to keep existing merchants unbroken and to gate the most sensitive scope (`read_orders`) behind Shopify's Protected Customer Data review:

| Step | What | State |
|---|---|---|
| 1 | Add 6 benign read scopes to the Shopify app | ✅ **shipped** (Cloud Run + Partner Dashboard) |
| 2 | Declare `read_orders` in `optional_scopes` to validate Shopify accepts it | ✅ **shipped** |
| 3 | Backend Postgres persistence for attributed orders | ✅ **shipped** (migration applied, code deployed) |
| 3.5 | Cart-attribute stamping in `chatbot.js` | ✅ **shipped** (commit `d22f151`) |
| — | Shopify PCD Level 1 review | ✅ **approved** — merchants can and do grant `read_orders` |
| 4 | Dashboard "Sales attribution" CTA + revenue widgets + Sankey extension | ✅ **shipped 2026-06-10** — CTA, Total Revenue + AOV tiles, product breakdown, funnel "Purchased" stage |
| 5 | BigQuery export pipeline (Postgres → BQ → Databricks) | 🔲 blocked on BQ table creation |
| 6 | Flip `read_orders` from optional → required + scope-drift auto-redirect | 🔲 not started — **now unblocked** (PCD approved) |

---

## What we're building, briefly

ChekOut AI's agent recommends products on a merchant's Shopify storefront. Until now we could measure adds-to-cart from the chat (via GA4), but not **purchases** — so we couldn't tell merchants "the agent drove $X in orders this month," the core ROI claim.

The fix: when the agent adds to cart, also stamp the cart with `_chekout_ai_session=<id>` (and a few sibling attrs). Those attributes ride into `order.note_attributes` when the customer checks out. With `read_orders`, we read them back, persist the attributed subset, and surface revenue + Sankey conversion to the merchant.

**Key constraint:** we operate at Shopify's Protected Customer Data **Level 1** — no customer PII (name/email/phone/address). Everything we read, store, project, and serve is PII-free by enforced field allowlist. Retention is 60 days. This is what we attested to with Shopify.

---

## Where we are right now

### Production state (Cloud Run `shopify-sync`)

- **Service URL:** https://shopify-sync-579388332064.us-central1.run.app
- **Project:** `production-aibuilder` / **Region:** `us-central1`
- **Live scopes (required):** `read_inventory,read_product_listings,read_products,read_price_rules,read_discounts,read_gift_cards,read_legal_policies,read_shipping,read_content` — 9 benign read scopes, no PCD review needed.
- **Live scopes (optional, granted):** `read_orders` — PCD approved; granted in production by at least `rosendahl-design-group` and `nuthatch-naturals` (via the managed-install optional-scopes flow, self-healed through `/api/orders/scope-status`).
- **Cloud SQL instance:** `production-aibuilder:us-central1:chekout-db-prod`, database `chekoutai`, user `user_prod`.
- **Order endpoints (live and serving):**
  - `GET /api/orders/scope-status` → reports whether a merchant has `read_orders`
  - `GET /api/orders/attributed` → list of attributed orders from Postgres
  - `GET /api/orders/attribution-summary` → aggregate stats for dashboard widgets
- **All three are scope-gated**: clean 403 for merchants without `read_orders`, never a 500.
- **New webhook handlers:** `orders/create`, `orders/updated`, `orders/cancelled` (subscription is conditional on `has_scope(read_orders)` in `webhook_manager.py`).
- **Scheduled prune:** APScheduler runs `prune_orders` daily at 03:00 UTC, deletes rows older than 60 days.
- **GDPR webhooks** now delete orders on `shop/redact` (full) and `customers/redact` (by `orders_to_redact` list).

### Frontend state (`chekoutai-frontend`)

- `public/chatbot.js` already stamps cart attributes on every Shopify add-to-cart (commit `d22f151`). Fire-and-forget; can't break add-to-cart. **Verified deployed** — `tagShopifyCartAttribution` and `_chekout_ai_session` are both present in the live bundles at `ai-builder.chekout.ai/chatbot.js` and `app.chekout.ai/chatbot.js`.
- **Dashboard sales-attribution UI shipped 2026-06-10** (Track 2 — see that section for what landed). One follow-up tabled: "Deferred to next build".

### PCD review — ✅ APPROVED

Approved since this doc was last written. The optional-scope grant flow works in
production: `[Scope Reconcile]` + `[Provision Orders]` fired for real merchants,
`orders/*` webhooks registered, and order webhooks are being delivered and
processed. Track 6 (flip to required) is unblocked.

Original submission details, kept for the record:

- **Submitted:** 2026-05-28
- **Level:** 1 (no customer PII)
- **Retention attestation:** 60 days
- **Expected response window:** several business days to ~2 weeks; may include clarifying questions.
- **What to check:** Partner Dashboard → ChekOut AI → API access → Protected customer data → status flips from *Pending review* to *Approved*. Email notification on decision.

---

## 2026-08-24 investigation — the pipeline works, the funnel doesn't

Started as "is the attributed-orders pipeline working?" It is. The finding is
what that reveals about the product, plus four bugs found along the way.

### What was confirmed working

Traced end to end against production logs and a real Postgres:

- **The grant flow works.** `[Scope Reconcile] rosendahl-design-group scope updated -> ...,read_orders`
  then `[Provision Orders] Starting`. The managed-install optional-scopes grant never
  calls `/api/oauth/complete`, and the self-healing pull in `/api/orders/scope-status`
  correctly caught it.
- **`orders/*` webhooks are registered and delivering.** Both `orders/create` and
  `orders/updated` arrive and are processed.
- **The ingest code is correct.** Exercised end to end against Postgres with synthetic
  orders (37 checks): attribution parsing (both `_chekout_ai_session` and the bare form),
  all three webhook topics, upsert-in-place with no duplicate rows, the read endpoints
  and their revenue math (paid-only summation verified), scope-gated 403s, the 60-day
  prune, and GDPR order deletion. All pass.
- **The stamping code is live** on both CDN bundles.

### The numbers

```
orders/create events, 60 days:   rosendahl-design-group   86
                                 pacsoulfoods             60
                                 blackcat-mfg              2
                                 nuthatch-naturals         1
                                 ---------------------------
                                 total                   149

attributed orders persisted:       0
nightly prune:                     "Deleted 0 orders" every night
```

**149 real orders. Zero carried a `_chekout_ai_*` attribute.**

### Why that means what it means

For Shopify, the widget's add-to-cart is the **only** path from agent to purchase.
The product card click opens an in-widget detail panel (`e.stopPropagation()` in
`InlineProductCarousel.tsx`), and `QuantitySelector` uses `product_url` only to derive
the shop origin, never to navigate. (There *is* a `window.location.href = productUrl`
in `chatbot.js`, but it's inside a Squarespace-only branch for products needing variant
options — it does not apply here.)

Cart attributes persist on the cart until checkout, so anyone who ever clicked widget
add-to-cart and later completed checkout **would** have attributed, whatever else they
did in between. Zero across 149 orders is therefore a real zero, not a sampling artifact.

### ⚠️ The one number that isn't in this repo

The widget fires GA4 telemetry on every add-to-cart
(`InlineProductCarousel.tsx:75` → `lib/analytics.ts::trackAddToCartClicked`):

```ts
trackEvent('add_to_cart_clicked', merchantId, { session_id, product_name, product_id, quantity, variant_id })
```

**Query `add_to_cart_clicked` in GA4 for the last 60 days.** It splits the problem three ways:

| `add_to_cart_clicked` | Attributed orders | What it means | Where to work |
|---|---|---|---|
| ≈ 0 | 0 | Nobody presses the button. The agent isn't driving intent to purchase. | Widget UX / agent prompting — **not** attribution |
| \> 0 | 0 | People add via widget then abandon before checkout. | Checkout funnel; size the drop-off |
| \> 0 | 0, but stamping suspected | `/cart/update.js` failing silently (it's fire-and-forget with only a `console.warn`) | Instrument `tagShopifyCartAttribution` |

Until that number is known, "nobody is converting" and "nobody is even trying" are
indistinguishable, and they have completely different fixes.

### 🐞 Bug: the session id can't join to anything

`models.py` calls `chekout_ai_session` *"the JOIN key with chatbot conversation data"* and
Track 3's Sankey (`add_to_cart → purchase`) is built on it. **It cannot join.** The two
sides mint different ids:

| | widget (`lib/getSessionId.ts`) | storefront (`public/chatbot.js`) |
|---|---|---|
| storage key | `{merchantId}_chat_session_id` | `checkout_ai_session_id` |
| generator | `nanoid()` | `'cai_' + Date.now().toString(36) + '_' + rand` |
| example | `V1StGXR8_Z5jdHi6B-myT` | `cai_m1x2y3_a1b2c3d4` |
| lands in | GA4 `add_to_cart_clicked.session_id` | `orders.chekout_ai_session` |

They also can't be reconciled after the fact: the widget runs in an iframe on
`app.chekout.ai`, `chatbot.js` runs on the merchant's storefront, and `sessionStorage`
is per-origin — neither can read the other's value even if the key names matched.

**Fix:** one side mints the id and hands it to the other. `chatbot.js` already
`postMessage`s to the iframe, so pass the id across at handshake and have both the GA4
event and the cart attribute use it. **Do this before Track 3** — the BigQuery Sankey
JOIN is dead on arrival otherwise.

### 🐞 Bug: Cloud Run is CPU-throttling every background task

The service has `startup-cpu-boost: 'true'` but **no `run.googleapis.com/cpu-throttling: 'false'`**,
so it takes the default: CPU is allocated only during request processing. FastAPI
`BackgroundTasks` run *after* the response is sent, so they are starved.

Evidence — `[Provision Orders] Starting` → `Failed` took **9m29s** (rosendahl) and
**12m17s** (nuthatch) for work that takes seconds locally.

This affects **every** background task in the service:
`initial_product_sync_background` (the OAuth product sync), `_background_product_sync`
(custom app), `provision_order_access_background`, `_oauth_order_backfill_background`.
Very likely why product syncs appeared to run without landing.

**Fix:** redeploy with `--no-cpu-throttling`. Deploy-flag change, no code.

### 🐞 Bug: the 60-day backfill has never succeeded

For both merchants that granted:

```
ERROR [Order Backfill] Failed for rosendahl-design-group:
      {'status': 'error', 'scanned': 0, 'persisted': 0, 'error': ''}
```

The empty `error` is diagnostic: `str(httpx.ReadTimeout())` is `''` (same for
`ConnectTimeout`, `ReadError`). It timed out — at the time (2026-07-28/29)
`make_shopify_request` was still a bare `httpx.AsyncClient()` on httpx's 5s default.
The 30s timeout + bounded retry (`e2f85f3`) only landed 2026-08-06 and prod picked it
up in revision `shopify-sync-00024-7z4` on 2026-08-12.

So the pre-grant 60-day window was never captured for either merchant. Moot today
(nothing was attributed anyway), but **re-run the backfill after fixing CPU throttling**.

Related: `backfill_attributed_orders` walks a single page of 250 with no `since_id`
pagination. The docstring justifies this as "far fewer than 250 attributed orders" —
but the cap is on **orders scanned**, not attributed. A store with 1,000 orders in the
window gets 250 scanned and misses the rest. There's also no explicit `order=` param,
so which 250 depends on Shopify's default sort.

### 🐞 Bug: you can't tell "working with zero" from "not running"

`_handle_order_event` logs only on the *not-attributed* branch — the success path logs
nothing. That's precisely why this took a full day to diagnose. Add a log line and a
counter on successful attribution.

---

## ⚠️ Important: branch state

**None of the step 1/2/3 commits are on `main` yet.** Production has been deployed via `./deploy.sh production` directly from feature branches. If anyone pushes to `main` without merging these first, the next `./deploy.sh production` from `main` will **regress production to pre-step-1 code**.

The stack, oldest → newest:

```
main (859a164)  ──  pre-step-1 production state
  └─ scopes/benign-read-only (8e718d9)         step 1
       └─ scopes/add-read-orders-optional (a76de21)   step 2
            └─ feat/orders-postgres-persistence (b3fccad)   step 3 ← current HEAD
```

**Recommended cleanup before doing anything else on main:** fast-forward merge `feat/orders-postgres-persistence` → `main` (or open one PR per step if you want reviewable history) and push. Then `main` reflects what's actually in production.

---

## Uncommitted work in the tree (2026-08-24)

Sitting on `feat/orders-postgres-persistence`, **not committed, not deployed**. Suggested
as three commits.

**1. Product sync: page-batched writes + failsafes** — `product_sync.py`, `product_reconciliation.py`, `sync.py`

`sync_products()` looked each product up twice (once in the loop, once inside
`upsert_product`) and committed + refreshed per product — ~5 DB round trips each, ~1,250
for a 250-product page. Now: one embedding batch, one SELECT to classify created-vs-updated,
one `INSERT ... ON CONFLICT` for the page, one commit. Measured **48–63× faster**
(372ms → 7.7ms per product; a 250-page now takes ~0.5s).

Failsafes added, because the danger isn't a failed write (atomic, rolls back) but a
*successful* write of garbage:
- Columns are written only when their source key is present in the payload, so a
  `?fields=id,title` response can't blank `vendor`/`handle`/`status`/`raw_data`.
  Present-but-null still writes, so unpublishing works.
- A partial payload may update an existing product but never create one.
- `embedding = COALESCE(excluded.embedding, products.embedding)` — a failed Vertex batch
  can't null out stored vectors.
- Reconciliation refuses to soft-delete >10% of a catalog in one pass (floor 10), and
  flatly refuses when Shopify returns 0 products against a non-empty DB. `force_delete=true`
  overrides.
- A run that stops making progress aborts instead of burning through every page.

Also fixed: reconciliation was fetching `fields=id,title,updated_at` and **upserting those
skinny payloads over fully-synced rows**, nulling vendor/handle/status/`raw_data` on every
product it touched. It now fetches full payloads (same API call count).

Verify with `PYTHONPATH=. python scripts/verify_product_sync.py` — 49 checks against a
real Postgres, cleans up after itself. Point it at dev, not prod.

**2. Tenant-filter fixes (9 sites)** — `sync.py` ×5, `webhooks.py` ×3, `variants.py` ×1

Fallout from migration 003 (2026-01-13, `b6ed746`), which renamed the integer
`products.merchant_id` → `store_id` and added a VARCHAR `merchant_id`. The services were
updated; **the routers weren't.** Every one of these raised
`operator does not exist: character varying = integer` — a hard 500, for ~7 months:

| Site | Broken behaviour |
|---|---|
| `sync.py` ×5 | `GET /api/sync/status` 500s |
| `variants.py` | `GET /api/variants/{id}` 500s — no variant/SKU/inventory lookups |
| `webhooks.py:211` | **`products/delete` never soft-deleted anything** — the agent keeps recommending removed products |
| `webhooks.py:519,736` | **GDPR `shop/redact` 500s** — products never redacted |

⚠️ The `shop/redact` fix also unblocks orders. `delete_orders_for_merchant()` sits one line
*below* the failing product query in the same `try`, so it has never executed. The PCD
attestation "we erase order data on uninstall" starts being true only with this fix.

**3. Orders: `updated_at` in the field allowlist** — `order_attribution.py`

`_ORDER_FIELDS` never requested `updated_at`, but `parse_order_for_db` reads it — so every
order from the backfill or live-read path landed with `shopify_updated_at = NULL`. Webhook
orders were unaffected (full body). Allowlist re-checked: still no PCD field.

---

## Architecture decisions (and why)

Decisions that aren't obvious from the code — written down so future-you/me doesn't second-guess them six months from now.

**Optional scope, not required.** Existing merchants keep working with their old tokens; new grants happen via a per-merchant CTA only after PCD clears. Forcing it required pre-approval would block new installs.

**No customer PII at any layer.** Field allowlist enforced from the Shopify API request (`fields=` query param) → parser (drops everything not in the allowlist) → DB columns (no `customer`, `email`, `*_address` columns exist) → projection to BQ later (same allowlist). Three layers of "don't even fetch it." Lets us request Level 1 PCD only, skip the security review.

**Cart-level attributes, not line-item properties.** Line-item properties cause Shopify to split line items (same product + different properties = two rows in the cart UI). Cart attributes are invisible to customers and don't split. `order.note_attributes` is the read-back path.

**Underscore-prefixed attribute keys (`_chekout_ai_*`).** Underscore prefix hides the attribute from the customer in Shopify's UI rendering. Pure cosmetics; parser strips the underscore before matching.

**Postgres as source of truth, BigQuery as analytics projection.** Postgres handles upserts (refunds, status changes) cleanly. BQ gets a slim, partitioned, expire-able projection (8 cols vs 18). Compliance: 60-day prune cron in Postgres + 60-day partition expiry in BQ — both expire the same data the same day.

**Persist only attributed orders.** If `note_attributes` carries no `_chekout_ai_*` key, the webhook handler returns 200 without writing. Data minimization in practice: we hold what we use.

**`event_type` per row, no event-sourcing.** Postgres upserts replace mutable fields (status, totals, attribution, line items) on `orders/updated` and `orders/cancelled` webhooks. The `last_event_type` column records what last touched the row. Simpler than an event log; sufficient for the dashboard's needs.

---

## Open tracks — pick up here

### Track 2 — Dashboard "Sales attribution" CTA + widgets ✅ SHIPPED 2026-06-10

Repo: `chekoutai-frontend` (Next.js)

**Shipped to production 2026-06-10** (Cloud Run `nextjs` + Firebase Hosting, `app.chekout.ai`):
- `POST /api/oauth/upgrade-scopes` (shipped in `shopify-sync`, rev `shopify-sync-00018-bvd`) builds the optional-scope grant URL.
- `lib/attribution.ts` — shared scope-status + summary fetch, currency/AOV helpers, attributed-orders fetch + `aggregateTopProducts` / `countPaidOrdersInRange`.
- `SalesAttribution` card — CTA when ungranted; KPIs + **Top products purchased** breakdown when granted.
- `TopMetrics` — real attributed revenue + **Average Order Value** tile (replaced cost-per-consumer); count-up tickers.
- `ConversionFunnel` — **Purchased** stage from date-range paid attributed orders (clamped to nest under Add to Cart; label shows true count).
- Fixed an auth-token race (`getFirebaseIdToken` now waits for Firebase's first auth-state resolution) that 401'd the dashboard fetch on hard-refresh.

Frontend commits on `chekoutai-frontend@main`: `99f80a8`, `9ba7af0`, `731ee2b`, `c5a8d53`, `ae6149d`. Original to-build notes below kept for reference.

**Backend (one small new endpoint in `shopify-sync`):**
- `POST /api/oauth/upgrade-scopes?merchant_id=...` — builds the OAuth authorize URL for that merchant with `read_orders` appended to the existing scope set. Returns the URL. Frontend redirects to it; Shopify's standard consent screen renders; on return, the existing `/api/oauth/complete` flow already updates `merchant.scope`.
- The OAuth-builder helper is `app/services/shopify_oauth.py::ShopifyOAuth.get_authorization_url(...)` — already exists, just needs to accept an extra-scopes argument.

**Frontend pieces:**
- New card component, "Sales attribution":
  - On mount: hits `/api/orders/scope-status` with the merchant header.
  - If `granted: false` → render the "Enable sales attribution" CTA with the subtext we agreed on: "See which orders came from agent-assisted sessions, including line items, discounts used, and revenue. Requires a one-time permission grant from Shopify."
  - If `granted: true` → render "Active ✓" + a KPI preview (orders attributed, revenue) pulled from `/api/orders/attribution-summary`.
  - "Enable" → hits the new `/api/oauth/upgrade-scopes` endpoint, redirects the merchant to the returned URL.
- Revenue widget on the main dashboard — wires to `/api/orders/attribution-summary` for total revenue + units + product count.
- Sankey extension in `components/Dashboard/ConversionFunnel/ConversionFunnel.tsx` — currently ends at `add_to_cart`; extend to `add_to_cart → purchase → revenue` via a JOIN on `chekout_ai_session` (only possible once Track 3 lands Postgres → Databricks; until then, the Sankey shows the existing 4 stages and the new stages render as "Pending — enable sales attribution to see").

**Effort:** ~1–2 days. Can fully ship UI before PCD approves (widgets show zeros until merchants grant).

### Track 3 — BigQuery export pipeline (Postgres → BQ → Databricks)

> ⚠️ **Second blocker, added 2026-08-24:** the `chekout_ai_session` JOIN key this track's
> Sankey depends on **does not join** — the widget and `chatbot.js` mint different session
> ids in different origins. Fix that first (see "Bug: the session id can't join to
> anything"), or the BQ table will land with a key that correlates to nothing. Also note
> there is currently **nothing to export** — `shopify_sync.orders` is empty.

**Blocking dep:** data engineer must create the BigQuery table. Schema to request (paste this into the ticket):

```sql
CREATE TABLE `<project>.chekoutai_attribution.shopify_orders` (
  merchant_id        STRING    NOT NULL,
  shopify_order_id   INT64     NOT NULL,
  created_at         TIMESTAMP NOT NULL,
  chekout_ai_session STRING,
  total_price        NUMERIC,
  currency           STRING,
  financial_status   STRING,
  line_items         ARRAY<STRUCT<
                       product_id INT64,
                       variant_id INT64,
                       title      STRING,
                       sku        STRING,
                       quantity   INT64,
                       price      NUMERIC
                     >>
)
PARTITION BY DATE(created_at)
CLUSTER BY merchant_id, chekout_ai_session
OPTIONS (
  partition_expiration_days = 60,
  description = "Agent-attributed Shopify orders projected from shopify-sync Postgres. 60-day rolling retention per PCD attestation."
);
```

**Permissions to grant the shopify-sync service account:**
- `roles/bigquery.dataEditor` on the dataset/table
- `roles/bigquery.jobUser` on the project (needed for streaming inserts)

The exact service account email is on the Cloud Run service: `gcloud run services describe shopify-sync --region=us-central1 --format='value(spec.template.spec.serviceAccountName)'`

**Also confirm with the engineer:** Databricks ETL needs to ingest this BQ table the same way it ingests the GA4 export. Once that's wired, the Sankey JOIN works.

**`shopify-sync` build (after table exists):**
- New `app/services/bigquery_sink.py` — wraps `google-cloud-bigquery` streaming insert.
- New scheduled job in `app/services/scheduler.py` (suggest hourly): query Postgres orders ingested since last export → project to 8 cols → streaming insert. Track last-export watermark in a small `etl_state` table (one row) to keep it simple.
- Pin `google-cloud-bigquery` in `requirements.txt`.

**Effort:** ~3 days after BQ table exists.

### Track 6 — Flip required, scope-drift middleware (post-PCD)

Only after PCD is approved:

1. Move `read_orders` from `optional_scopes` to `scopes` in `shopify.app.toml`.
2. Add `read_orders` to `SHOPIFY_SCOPES` in `app/config.py` + `.env.example` + `.env.local`.
3. Run `shopify app deploy` to publish new app version.
4. Run `./deploy.sh production`.
5. Add scope-drift middleware in shopify-sync that, on any authenticated request, compares `merchant.scope` to required scopes; if `read_orders` missing, returns a 409 with the OAuth re-auth URL. The dashboard intercepts the 409 and redirects.

**Note:** before flipping required, verify on a test store that:
- Fresh install requests `read_orders` and Shopify's consent screen shows it.
- Granted token includes `read_orders` in the `merchant.scope` row.
- Webhook subscription for `orders/*` succeeds.
- Backfill task triggers and writes rows.
- Dashboard widget shows non-zero numbers.

---

## Files & commits reference

### Branches (in shopify-sync)
| Branch | Commit | Purpose | Deployed |
|---|---|---|---|
| `scopes/benign-read-only` | `8e718d9` | 6 benign scopes + scope helpers | Cloud Run |
| `scopes/add-read-orders-optional` | `a76de21` | `read_orders` in `optional_scopes` | Partner Dashboard (`shopify app deploy`) |
| `feat/orders-postgres-persistence` | `b3fccad` | Postgres table + webhooks + backfill + prune + read endpoints | Cloud Run |

### Key new/changed files
| File | What |
|---|---|
| `migrations/005_add_orders_table.sql` | Schema for `shopify_sync.orders` (23 cols, 7 indexes, PII-free) |
| `app/models.py` (`Order` class) | SQLAlchemy model |
| `app/services/order_persistence.py` | Parse / upsert / backfill / prune / redact |
| `app/services/order_attribution.py` | Field allowlist + attribute extraction (parser shared between live read and persistence) |
| `app/services/webhook_manager.py` | `ORDERS_WEBHOOK_CONFIG`, conditional subscription based on `has_scope(read_orders)` |
| `app/routers/webhooks.py` | New `orders/create/updated/cancelled` handlers; `shop/redact` + `customers/redact` extended |
| `app/services/scheduler.py` | Daily 03:00 UTC `run_daily_order_prune` |
| `app/routers/orders.py` | `/scope-status`, `/attributed`, `/attribution-summary` (Postgres-backed) |
| `app/routers/oauth.py` + `app/routers/custom_app.py` | Backfill background task triggered when `read_orders` is in granted scopes |
| `app/utils/helpers.py` | `parse_scopes`, `has_scope` |
| `shopify.app.toml` | `scopes` = 9 benign; `optional_scopes` = `["read_orders"]` |
| `app/config.py` + `.env.example` + `.env.local` | `SHOPIFY_SCOPES` updated; `SHOPIFY_OPTIONAL_SCOPES` added |

In `chekoutai-frontend`:
| File | What |
|---|---|
| `public/chatbot.js` (commit `d22f151`) | `getCheckoutAiSessionId` + `tagShopifyCartAttribution`, called after `/cart/add.js` success |

---

## Operational cheatsheet

### Deploy backend to Cloud Run

```bash
echo yes | ./deploy.sh production
```

Pulls scopes from `.env.local`. CI workflow (`.github/workflows/deploy.yml`) exists but is broken — don't use it. Deploys are manual via the script.

### Apply a new SQL migration to prod Postgres

Cloud SQL uses Unix-socket auth via the Cloud SQL Auth Proxy. From this repo:

```bash
ARCH=$(uname -m | sed 's/x86_64/amd64/;s/arm64/arm64/')
curl -fsSL "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.11.4/cloud-sql-proxy.darwin.${ARCH}" -o /tmp/cloud-sql-proxy
chmod +x /tmp/cloud-sql-proxy
TOKEN=$(gcloud auth print-access-token)
/tmp/cloud-sql-proxy --port 5433 --token "$TOKEN" production-aibuilder:us-central1:chekout-db-prod > /tmp/csql-proxy.log 2>&1 &
PROXY_PID=$!
sleep 3

source venv/bin/activate
python << 'PYEOF'
import psycopg2, subprocess, urllib.parse
dsn = subprocess.check_output(['gcloud','secrets','versions','access','latest','--secret=DB_DSN_PROD','--project=production-aibuilder'], text=True).strip()
p = urllib.parse.urlparse(dsn)
conn = psycopg2.connect(host='127.0.0.1', port=5433, user=p.username, password=urllib.parse.unquote(p.password), dbname=p.path.lstrip('/'))
conn.autocommit = True
with open('migrations/XYZ.sql') as f:
    conn.cursor().execute(f.read())
print('done')
PYEOF

kill $PROXY_PID
rm -f /tmp/cloud-sql-proxy /tmp/csql-proxy.log
```

### Push a new Shopify app version

```bash
shopify app deploy --force --message "<what changed>"
```

(May prompt for browser auth if the CLI session lapsed.)

### Pull a Secret Manager value

```bash
gcloud secrets versions access latest --secret=<NAME> --project=production-aibuilder
```

Useful secret names: `DB_DSN_PROD`, `API_KEY_PROD`, `SHOPIFY_API_KEY_PROD`, `SHOPIFY_API_SECRET_PROD`, `ENCRYPTION_KEY`.

### Hit the new endpoints

```bash
BASE="https://shopify-sync-579388332064.us-central1.run.app"
API_KEY=$(gcloud secrets versions access latest --secret=API_KEY_PROD --project=production-aibuilder)
curl -sS -H "X-API-Key: $API_KEY" -H "X-Merchant-Id: <merchant>" \
  "$BASE/api/orders/scope-status"
```

(Note: the merchant header is `X-Merchant-Id`, despite the error message saying `X-ShopifyStore-Id`. See "Known issues" below.)

---

## Deferred to next build

Intentionally tabled — not blockers, scoped for the next frontend build.

### Hold Revenue + AOV tiles until attribution resolves (frontend)

**Added:** 2026-06-10.

**What:** On the dashboard, the **Total Revenue** and **Average Order Value** tiles fall back to the GA4 estimate (`dashboardData.total_revenue`) while the attribution fetch is still in flight, then glide to the real Postgres-backed attributed numbers once `/api/orders/attribution-summary` resolves. For a *granted* merchant this briefly shows the fallback figure before settling on the attributed value.

**Desired:** keep just those two tiles in their skeleton/loading state until attribution has resolved, so there's no fallback flash — the number appears once and counts up a single time.

**Where:** `chekoutai-frontend` → `components/Dashboard/TopMetrics/TopMetrics.tsx`. It already receives the `attribution` prop (which has `.loading`). Pass `isLoading={loading || attribution?.loading}` to the **Total Revenue** and **Average Order Value** `<MetricCard>`s only. `MetricCard` already renders a skeleton for `isLoading`.

**Watch outs:**
- Do **not** gate Total Conversations / Active Sessions on attribution — they come from the base dashboard fetch and have nothing to do with `read_orders`.
- Gate on `attribution.loading`, **not** `granted`: for ungranted merchants `attribution.loading` flips false once scope-status resolves (granted:false), so those tiles correctly fall through to the GA estimate as they do today.

**Acceptance:** granted-merchant hard-refresh → Revenue + AOV show skeleton until the attributed number is ready, then count up once (no fallback flash); Conversations/Active Sessions unaffected.

**Effort:** ~15 min, frontend-only. Ship in the next `./deploy.production.sh` for the frontend.

---

## Known issues / minor cleanups

1. **`app/middleware/auth.py:31` docstring/error says `X-ShopifyStore-Id`, actual header is `X-Merchant-Id`.** Pre-existing. Fix is either (a) update the error message + docstring, or (b) add `Header(..., alias="X-ShopifyStore-Id")` to the param. Not blocking anything.
2. **`.github/workflows/deploy.yml` is dead.** Either fix it or delete it. While dead it's also a footgun — the `SHOPIFY_SCOPES=${{ vars.SHOPIFY_SCOPES }}` line would clobber Cloud Run's env var with the empty string if anyone ever runs it. Surgical fix: remove the `SHOPIFY_SCOPES=` injection from lines 159 + 208.
3. **Step branches not merged to main.** See the "Branch state" warning above. Merge them or delete them to avoid a future regression on a fresh `main` deploy.
4. **`backfill_attributed_orders` walks one page of 250 with no pagination.** The cap is on
   orders *scanned*, not attributed, and there's no explicit `order=` param — so on a busy
   store you get an arbitrary 250 and miss the rest. Add `since_id` pagination.
5. **`_handle_order_event` doesn't log the success path**, so "working with zero attributions"
   is indistinguishable from "not running" in Cloud Run logs. Add a log line + counter.
6. **Custom-app merchants' `merchant.scope` is stale until they reconnect.** Old custom-app merchants have the old hardcoded scope string. The new code now reads real granted scopes via `/admin/oauth/access_scopes.json`, but only on new connects. Optional: write a one-off backfill that calls `get_access_scopes` for each existing custom-app merchant and updates their `scope` column. Not blocking.

---

## How to resume

The attribution *plumbing* is done. The open question is no longer "does it work" but
"is anyone converting" — and the next three steps are ordered by what unblocks the most.

1. **Get the GA4 `add_to_cart_clicked` count for the last 60 days.** See the table in
   the 2026-08-24 section. Until you have it you cannot tell a conversion problem from a
   product problem, and you'd be guessing about what to build next. Nothing else on this
   list matters as much.

2. **Redeploy with `--no-cpu-throttling`.** One flag. Un-starves every background task in
   the service — OAuth product sync, custom-app sync, order provisioning, order backfill.
   Then re-run the backfill for `rosendahl-design-group` and `nuthatch-naturals`.

3. **Land the uncommitted work** (see "Uncommitted work in the tree"). The nine
   tenant-filter fixes are the urgent half — `products/delete` and GDPR `shop/redact`
   have been failing since January.

Then, depending on what step 1 says:

- **If `add_to_cart_clicked` is ≈ 0** — this is not an attribution problem. The agent
  isn't converting browse into cart-adds. Work the widget UX; the measurement stack is
  already ahead of the product.
- **If it's meaningfully > 0** — unify the session id (see the JOIN-key bug) so orders can
  be traced back to conversations, then Track 3 (BigQuery) becomes worth building.

Track 6 (flip `read_orders` to required) is unblocked by PCD approval but is low value
until there's something to attribute.

If picking up with Claude, paste this doc into the new session and say "we're resuming
sales attribution from this handoff doc, pick up at step N."
