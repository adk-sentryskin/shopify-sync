#!/usr/bin/env python3
"""
Product sync verification suite.

Exercises the page-sync write path against a real Postgres, with an emphasis on
the ways a bad sync could destroy product data. Everything runs under throwaway
merchant ids prefixed `__synctest_` and is deleted again on the way out.

    PYTHONPATH=. python scripts/verify_product_sync.py

Runs against whatever DB_DSN resolves to, so point it at dev — not production.
Embeddings are stubbed, so it never calls Vertex AI.
"""

import argparse
import asyncio
import random
import sys

from app.config import settings

settings.ENABLE_EMBEDDINGS = False  # keep Vertex out of the loop

from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Product, ShopifyStore  # noqa: E402
from app.services import product_reconciliation as recon  # noqa: E402
from app.services import product_sync as ps  # noqa: E402

TEST_PREFIX = "__synctest_"
PAGE = 250

_failures = []
_checks = 0


def section(title):
    print(f"\n\033[1m{title}\033[0m")


def check(name, condition, detail=""):
    global _checks
    _checks += 1
    mark = "\033[32m PASS \033[0m" if condition else "\033[31m FAIL \033[0m"
    print(f" {mark} {name}" + (f"   {detail}" if detail else ""))
    if not condition:
        _failures.append(name)


def full_product(product_id, index, **overrides):
    """A complete Shopify product payload, as products.json returns it."""
    payload = {
        "id": product_id,
        "title": f"Product {index}",
        "vendor": "Acme",
        "product_type": "Widget",
        "handle": f"product-{index}",
        "status": "active",
        "created_at": "2026-01-01T00:00:00-05:00",
        "updated_at": "2026-01-02T00:00:00-05:00",
        "published_at": "2026-01-01T00:00:00-05:00",
        "tags": "alpha,beta",
        "body_html": "<p>A product description.</p>",
        "variants": [{
            "id": product_id + 1, "sku": f"SKU-{index}", "price": "29.99",
            "inventory_quantity": 7, "title": "Default Title",
        }],
    }
    payload.update(overrides)
    return payload


def partial_product(product_id, index, **overrides):
    """What a `fields=`-narrowed or truncated response looks like."""
    payload = {
        "id": product_id,
        "title": f"Product {index} PARTIAL",
        "updated_at": "2026-03-01T00:00:00-05:00",
    }
    payload.update(overrides)
    return payload


def make_store(db, merchant_id):
    store = ShopifyStore(
        merchant_id=merchant_id,
        shop_domain=f"{merchant_id}.myshopify.com",
        scope="read_products",
        is_active=1,
    )
    db.add(store)
    db.commit()
    db.refresh(store)
    return store


def cleanup(db):
    db.rollback()
    db.execute(text("DELETE FROM shopify_sync.products WHERE merchant_id LIKE :p"),
               {"p": TEST_PREFIX + "%"})
    db.execute(text("DELETE FROM shopify_sync.shopify_stores WHERE merchant_id LIKE :p"),
               {"p": TEST_PREFIX + "%"})
    db.commit()
    return db.execute(text("SELECT count(*) FROM shopify_sync.products WHERE merchant_id LIKE :p"),
                      {"p": TEST_PREFIX + "%"}).scalar()


# --------------------------------------------------------------------------- #
# 1. Page sync semantics
# --------------------------------------------------------------------------- #

def test_page_sync(db, base):
    section("1. Page sync — a whole Shopify page in one statement")
    store = make_store(db, TEST_PREFIX + "page_%d" % random.randint(10**8, 10**9))
    page = [full_product(base + i, i) for i in range(PAGE)]

    stats = ps.sync_products(db, store, page)
    check("a page of new products is all created",
          stats['synced_count'] == PAGE and stats['created_count'] == PAGE, str(stats))

    stats = ps.sync_products(db, store, [full_product(base + i, i, title=f"P{i} v2")
                                         for i in range(PAGE)])
    check("re-syncing the same page is all updates",
          stats['updated_count'] == PAGE and stats['created_count'] == 0, str(stats))
    check("the update actually landed",
          db.query(Product.title).filter(Product.shopify_product_id == base).scalar() == "P0 v2")

    mixed = page[125:] + [full_product(base + 5000 + i, i) for i in range(125)]
    stats = ps.sync_products(db, store, mixed)
    check("a mixed page splits created/updated correctly",
          stats['created_count'] == 125 and stats['updated_count'] == 125, str(stats))

    stats = ps.sync_products(db, store, page[:3] + page[:3])
    check("duplicate ids in one page don't abort the statement",
          stats['synced_count'] == 3 and stats['failed_count'] == 0, str(stats))

    stats = ps.sync_products(db, store, page[:2] + [{"title": "no id"}])
    check("a product with no id is skipped, the rest still write",
          stats['synced_count'] == 2 and stats['failed_count'] == 1, str(stats))

    check("an empty page is a no-op", ps.sync_products(db, store, [])['synced_count'] == 0)


# --------------------------------------------------------------------------- #
# 2. Failsafe: an incomplete payload must never destroy data
# --------------------------------------------------------------------------- #

def test_partial_payload_failsafe(db, base):
    section("2. Failsafe — an incomplete payload cannot blank or invent a product")
    store = make_store(db, TEST_PREFIX + "partial_%d" % random.randint(10**8, 10**9))
    ps.sync_products(db, store, [full_product(base + i, i) for i in range(20)])
    db.execute(text("UPDATE shopify_sync.products SET embedding = :v WHERE merchant_id = :m"),
               {"v": "[" + ",".join(["0.3"] * 768) + "]", "m": store.merchant_id})
    db.commit()

    ps.sync_products(db, store, [partial_product(base + i, i) for i in range(20)])
    db.expire_all()
    row = db.query(Product).filter(Product.shopify_product_id == base).first()
    check("the field it carries is updated", row.title == "Product 0 PARTIAL", row.title)
    check("vendor is preserved", row.vendor == "Acme", repr(row.vendor))
    check("product_type is preserved", row.product_type == "Widget", repr(row.product_type))
    check("handle is preserved", row.handle == "product-0", repr(row.handle))
    check("status is preserved", row.status == "active", repr(row.status))
    check("published_at is preserved", row.published_at is not None)
    check("raw_data is not overwritten", bool(row.raw_data.get("variants")),
          str(sorted(row.raw_data.keys())))
    check("the embedding is preserved", db.execute(text(
        "SELECT embedding IS NOT NULL FROM shopify_sync.products WHERE shopify_product_id = :p"),
        {"p": base}).scalar() is True)

    stats = ps.sync_products(db, store, [partial_product(base + 9000 + i, i) for i in range(2)])
    check("an unknown product from a partial payload is refused",
          stats['skipped_partial_count'] == 2 and stats['synced_count'] == 0, str(stats))
    check("no half-empty row was created",
          db.query(Product).filter(Product.shopify_product_id == base + 9000).first() is None)

    stats = ps.sync_products(db, store, [partial_product(base + 1, 1),
                                         partial_product(base + 9500, 1)])
    check("on a mixed page the update lands and the create is refused",
          stats['synced_count'] == 1 and stats['skipped_partial_count'] == 1, str(stats))

    ps.sync_products(db, store, [full_product(base + 2, 2, vendor="", published_at=None,
                                              status="draft")])
    db.expire_all()
    row = db.query(Product).filter(Product.shopify_product_id == base + 2).first()
    check("a complete payload can still genuinely clear a field", row.vendor == "")
    check("a complete payload can still unpublish", row.published_at is None)
    check("a complete payload can still change status", row.status == "draft", row.status)


# --------------------------------------------------------------------------- #
# 3. Failsafe: the single-product webhook path
# --------------------------------------------------------------------------- #

def test_webhook_failsafe(db, base):
    section("3. Failsafe — the webhook path holds the same line")
    store = make_store(db, TEST_PREFIX + "hook_%d" % random.randint(10**8, 10**9))
    ps.sync_products(db, store, [full_product(base + i, i) for i in range(3)])

    ps.upsert_product(db, store, partial_product(base, 0))
    db.expire_all()
    row = db.query(Product).filter(Product.shopify_product_id == base).first()
    check("a truncated webhook body updates only what it carries",
          row.title == "Product 0 PARTIAL" and row.vendor == "Acme"
          and bool(row.raw_data.get("variants")))

    try:
        ps.upsert_product(db, store, partial_product(base + 7000, 99))
        created = True
    except ValueError:
        created = False
    check("a truncated webhook body cannot create a product", not created)

    try:
        ps.upsert_product(db, store, {"title": "no id"})
        accepted = True
    except ValueError:
        accepted = False
    check("a webhook body with no id is refused", not accepted)


# --------------------------------------------------------------------------- #
# 4. Failsafe: bulk deletion bound in reconciliation
# --------------------------------------------------------------------------- #

def test_delete_bound(db, base):
    section("4. Failsafe — reconciliation refuses a suspicious mass deletion")
    store = make_store(db, TEST_PREFIX + "del_%d" % random.randint(10**8, 10**9))
    products = [full_product(base + i, i) for i in range(150)]
    ps.sync_products(db, store, products)
    active = lambda: db.query(Product).filter(          # noqa: E731
        Product.merchant_id == store.merchant_id, Product.is_deleted == 0).count()

    def stub(payload):
        async def _fetch(shop_domain, access_token):
            return payload
        recon.fetch_all_products_from_shopify_for_reconciliation = _fetch

    stub([])
    r = asyncio.run(recon.reconcile_products(db, store, store.shop_domain, "t", mark_deleted=True))
    check("an empty Shopify response deletes nothing",
          r.get('mark_deleted_skipped') is True and r['marked_deleted_count'] == 0)
    check("the whole catalog is still active", active() == 150, str(active()))

    stub(products[:100])                                 # 50 missing = 33%
    r = asyncio.run(recon.reconcile_products(db, store, store.shop_domain, "t", mark_deleted=True))
    check("a 50-of-150 deletion is refused",
          r.get('mark_deleted_skipped') is True and r['marked_deleted_count'] == 0)
    check("the report still names what it would have deleted", r['deleted_in_shopify'] == 50)
    check("nothing was deleted", active() == 150, str(active()))

    r = asyncio.run(recon.reconcile_products(db, store, store.shop_domain, "t",
                                             mark_deleted=True, force_delete=True))
    check("force_delete=True still allows a deliberate deletion",
          r['marked_deleted_count'] == 50, str(r['marked_deleted_count']))

    db.execute(text("UPDATE shopify_sync.products SET is_deleted = 0, status = 'active' "
                    "WHERE merchant_id = :m"), {"m": store.merchant_id})
    db.commit()
    stub(products[:136])                                 # 14 missing = 9%
    r = asyncio.run(recon.reconcile_products(db, store, store.shop_domain, "t", mark_deleted=True))
    check("a deletion under the bound proceeds normally",
          r['marked_deleted_count'] == 14 and not r.get('mark_deleted_skipped'),
          str(r['marked_deleted_count']))


# --------------------------------------------------------------------------- #
# 5. Failsafe: reconciliation writes complete rows
# --------------------------------------------------------------------------- #

def test_reconciliation_writes(db, base):
    section("5. Failsafe — reconciliation re-syncs without hollowing rows out")
    store = make_store(db, TEST_PREFIX + "recon_%d" % random.randint(10**8, 10**9))
    ps.sync_products(db, store, [full_product(base + i, i) for i in range(5)])

    shopify_side = [full_product(base + i, i) for i in range(5)]
    shopify_side[2] = full_product(base + 2, 2, title="Product 2 CHANGED",
                                   updated_at="2026-03-09T00:00:00-05:00")
    shopify_side += [full_product(base + 5, 5), full_product(base + 6, 6)]

    async def _fetch(shop_domain, access_token):
        return shopify_side
    recon.fetch_all_products_from_shopify_for_reconciliation = _fetch

    r = asyncio.run(recon.reconcile_products(db, store, store.shop_domain, "t"))
    check("2 missing + 1 drifted product were re-synced", r['synced_count'] == 3, str(r['synced_count']))

    db.expire_all()
    drifted = db.query(Product).filter(Product.shopify_product_id == base + 2).first()
    check("the drifted row got the new title", drifted.title == "Product 2 CHANGED", drifted.title)
    check("reconciliation did not wipe vendor", drifted.vendor == "Acme", repr(drifted.vendor))
    check("reconciliation did not wipe handle", drifted.handle == "product-2", repr(drifted.handle))
    check("reconciliation did not wipe status", drifted.status == "active", repr(drifted.status))
    check("reconciliation kept raw_data.variants", bool(drifted.raw_data.get("variants")))

    created = db.query(Product).filter(Product.shopify_product_id == base + 5).first()
    check("a newly reconciled product is complete",
          created.vendor == "Acme" and bool(created.raw_data.get("variants")))


# --------------------------------------------------------------------------- #
# 6. Failsafe: degraded dependencies
# --------------------------------------------------------------------------- #

def test_degraded(db, base):
    section("6. Failsafe — degraded dependencies don't cost the page")
    store = make_store(db, TEST_PREFIX + "degr_%d" % random.randint(10**8, 10**9))
    page = [full_product(base + i, i) for i in range(10)]

    real = ps._execute_upsert

    def bulk_fails(session, rows):
        if len(rows) > 1:
            raise RuntimeError("simulated bulk failure")
        return real(session, rows)

    ps._execute_upsert = bulk_fails
    try:
        stats = ps.sync_products(db, store, page)
    finally:
        ps._execute_upsert = real
    check("a failed bulk statement falls back and still writes every row",
          stats['synced_count'] == 10, str(stats))

    def row_also_fails(session, rows):
        if len(rows) > 1:
            raise RuntimeError("simulated bulk failure")
        if rows[0]['shopify_product_id'] == base + 1:
            raise RuntimeError("simulated row failure")
        return real(session, rows)

    ps._execute_upsert = row_also_fails
    try:
        stats = ps.sync_products(db, store, page[:5])
    finally:
        ps._execute_upsert = real
    check("one poisoned row doesn't take the page down",
          stats['synced_count'] == 4 and stats['failed_count'] == 1, str(stats))

    settings.ENABLE_EMBEDDINGS = True
    original = ps.get_embedding_service

    class Broken:
        def prepare_product_text(self, pd): return "text"
        def generate_embeddings_batch(self, texts): raise RuntimeError("Vertex unavailable")

    ps.get_embedding_service = lambda: Broken()
    try:
        stats = ps.sync_products(db, store, page[:3])
    finally:
        ps.get_embedding_service = original
        settings.ENABLE_EMBEDDINGS = False
    check("an embedding outage doesn't stop the sync", stats['synced_count'] == 3, str(stats))

    settings.ENABLE_EMBEDDINGS = True

    class Working:
        def prepare_product_text(self, pd): return pd["title"]
        def generate_embeddings_batch(self, texts): return [[0.25] * 768 for _ in texts]

    ps.get_embedding_service = lambda: Working()
    try:
        ps.sync_products(db, store, page[:4])
    finally:
        ps.get_embedding_service = original
        settings.ENABLE_EMBEDDINGS = False
    written = db.execute(text("SELECT count(*) FROM shopify_sync.products "
                              "WHERE merchant_id = :m AND embedding IS NOT NULL"),
                         {"m": store.merchant_id}).scalar()
    check("vectors are written by the bulk statement", written == 4, f"count={written}")


# --------------------------------------------------------------------------- #
# 7. Failsafe: a multi-page run stops when it stops making progress
# --------------------------------------------------------------------------- #

def test_run_abort(db, base):
    section("7. Failsafe — a run that stops making progress aborts")
    store = make_store(db, TEST_PREFIX + "abort_%d" % random.randint(10**8, 10**9))
    pages_requested = []

    class FakeResponse:
        def __init__(self, payload): self._payload = payload
        def raise_for_status(self): pass
        def json(self): return self._payload

    class FakeClient:
        """Page 1 is healthy; every page after it is unusable."""
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None, params=None):
            pages_requested.append(params['since_id'])
            if len(pages_requested) == 1:
                return FakeResponse({"products": [full_product(base + i, i) for i in range(PAGE)]})
            offset = 20000 + len(pages_requested) * PAGE
            return FakeResponse({"products": [partial_product(base + offset + i, i)
                                              for i in range(PAGE)]})

    real_client = ps.httpx.AsyncClient
    ps.httpx.AsyncClient = FakeClient
    try:
        r = asyncio.run(ps.fetch_all_products_from_shopify(db, store, store.shop_domain, "token"))
    finally:
        ps.httpx.AsyncClient = real_client

    check("the run stops after the first unproductive page", r['pages_fetched'] == 2,
          str(r['pages_fetched']))
    check("it does not keep paginating", len(pages_requested) == 2, str(len(pages_requested)))
    check("the healthy page's products are kept", r['synced_count'] == PAGE, str(r['synced_count']))
    check("the run is not reported as cleanly completed", r['status'] == 'partial', r['status'])
    check("the error explains the abort", 'wrote no products' in r.get('error', ''),
          r.get('error', ''))
    check("skipped products are counted, not silently dropped",
          r['skipped_partial_count'] == PAGE, str(r['skipped_partial_count']))
    check("no junk rows reached Postgres",
          db.query(Product).filter(Product.merchant_id == store.merchant_id).count() == PAGE)


TESTS = {
    "page": test_page_sync,
    "partial": test_partial_payload_failsafe,
    "webhook": test_webhook_failsafe,
    "delete": test_delete_bound,
    "reconcile": test_reconciliation_writes,
    "degraded": test_degraded,
    "abort": test_run_abort,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=sorted(TESTS), nargs="*",
                        help="run only the named test groups")
    args = parser.parse_args()

    dsn = settings.DB_DSN.split("@")[-1]
    print(f"product sync verification — database {dsn}")

    db = SessionLocal()
    try:
        for index, (name, fn) in enumerate(TESTS.items()):
            if args.only and name not in args.only:
                continue
            # Disjoint id space per group so a crashed run can't collide.
            fn(db, 9_000_000_000_000 + (index + 1) * 10_000_000
               + random.randint(0, 10**6) * 10)
    finally:
        remaining = cleanup(db)
        print(f"\ncleanup: {remaining} test rows remaining")
        db.close()

    print()
    if _failures:
        print(f"\033[31m{len(_failures)}/{_checks} CHECKS FAILED\033[0m")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print(f"\033[32mall {_checks} checks passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
