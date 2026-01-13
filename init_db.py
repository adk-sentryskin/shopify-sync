"""
Database initialization script
Run this to create the database tables
"""
from app.database import engine, Base
from app.models import ShopifyStore, Product, Webhook

def init_database():
    print("Creating database tables...")
    Base.metadata.create_all(bind=engine)
    print("Database tables created successfully!")
    print("\nTables created:")
    print("- shopify_stores")
    print("- products")
    print("- webhooks")

if __name__ == "__main__":
    init_database()
