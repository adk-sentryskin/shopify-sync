# Sales Attribution — Handoff & Resume Doc

**Last updated:** 2026-05-29
**Owner:** namit
**Status:** In flight — backend persistence shipped; awaiting Shopify PCD review; dashboard CTA + BigQuery export still to build.

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
| — | Shopify PCD Level 1 review | ⏳ **awaiting Shopify** (submitted 2026-05-28) |
| 4 | Dashboard "Sales attribution" CTA + revenue widgets + Sankey extension | 🔲 not started |
| 5 | BigQuery export pipeline (Postgres → BQ → Databricks) | 🔲 blocked on BQ table creation |
| 6 | Flip `read_orders` from optional → required + scope-drift auto-redirect | 🔲 not started (post-PCD-approval) |

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
- **Live scopes (optional, declared):** `read_orders` — declared in Partner Dashboard but **no merchant has granted it yet** (and can't, until PCD clears).
- **Cloud SQL instance:** `production-aibuilder:us-central1:chekout-db-prod`, database `chekoutai`, user `user_prod`.
- **New endpoints (live, dormant until a merchant grants):**
  - `GET /api/orders/scope-status` → reports whether a merchant has `read_orders`
  - `GET /api/orders/attributed` → list of attributed orders from Postgres
  - `GET /api/orders/attribution-summary` → aggregate stats for dashboard widgets
- **All three are scope-gated**: clean 403 for merchants without `read_orders`, never a 500.
- **New webhook handlers:** `orders/create`, `orders/updated`, `orders/cancelled` (subscription is conditional on `has_scope(read_orders)` in `webhook_manager.py`).
- **Scheduled prune:** APScheduler runs `prune_orders` daily at 03:00 UTC, deletes rows older than 60 days.
- **GDPR webhooks** now delete orders on `shop/redact` (full) and `customers/redact` (by `orders_to_redact` list).

### Frontend state (`chekoutai-frontend`)

- `public/chatbot.js` already stamps cart attributes on every Shopify add-to-cart (commit `d22f151`). Fire-and-forget; can't break add-to-cart.
- No dashboard surface for sales attribution yet — Track 2 work.

### PCD review

- **Submitted:** 2026-05-28
- **Level:** 1 (no customer PII)
- **Retention attestation:** 60 days
- **Expected response window:** several business days to ~2 weeks; may include clarifying questions.
- **What to check:** Partner Dashboard → ChekOut AI → API access → Protected customer data → status flips from *Pending review* to *Approved*. Email notification on decision.

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

### Track 2 — Dashboard "Sales attribution" CTA + widgets

Repo: `chekoutai-frontend` (Next.js)

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

## Known issues / minor cleanups

1. **`app/middleware/auth.py:31` docstring/error says `X-ShopifyStore-Id`, actual header is `X-Merchant-Id`.** Pre-existing. Fix is either (a) update the error message + docstring, or (b) add `Header(..., alias="X-ShopifyStore-Id")` to the param. Not blocking anything.
2. **`.github/workflows/deploy.yml` is dead.** Either fix it or delete it. While dead it's also a footgun — the `SHOPIFY_SCOPES=${{ vars.SHOPIFY_SCOPES }}` line would clobber Cloud Run's env var with the empty string if anyone ever runs it. Surgical fix: remove the `SHOPIFY_SCOPES=` injection from lines 159 + 208.
3. **Step branches not merged to main.** See the "Branch state" warning above. Merge them or delete them to avoid a future regression on a fresh `main` deploy.
4. **Custom-app merchants' `merchant.scope` is stale until they reconnect.** Old custom-app merchants have the old hardcoded scope string. The new code now reads real granted scopes via `/admin/oauth/access_scopes.json`, but only on new connects. Optional: write a one-off backfill that calls `get_access_scopes` for each existing custom-app merchant and updates their `scope` column. Not blocking.

---

## How to resume

When you (or a fresh Claude session) come back:

1. **Read this doc top-to-bottom.**
2. **Check PCD review status** in Partner Dashboard → ChekOut AI → API access → Protected customer data. If approved, the path forward changes (Track 6 unblocks).
3. **Pick the next track:**
   - Default: Track 2 (Dashboard CTA) — most user-visible, doesn't require any external dependency.
   - If you've pinged the data engineer and the BQ table exists, you can start Track 3 in parallel.
4. **Before any new code change:** decide whether to first **merge step branches → main**. If yes, do that as a separate housekeeping commit so the audit trail is clean.

If picking up with Claude, paste this doc into the new session and say "we're resuming sales attribution from this handoff doc, pick up at <track X>." That's enough context for a clean restart.
