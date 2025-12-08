"""
Database initialization script
Run this to create the database tables
"""
from app.database import engine, Base
from app.models import Merchant, Product, Webhook

def init_database():
    print("Creating database tables...")
    Base.metadata.create_all(bind=engine)
    print("Database tables created successfully!")
    print("\nTables created:")
    print("- merchants")
    print("- products")
    print("- webhooks")

if __name__ == "__main__":
    init_database()
