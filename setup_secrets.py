"""
One-time setup script: creates the Databricks secret scope and stores the
Lakebase connection URL and password. Run this once to configure credentials.

Usage:
    python setup_secrets.py
"""
from databricks.sdk import WorkspaceClient
from databricks.sdk.service import workspace
from getpass import getpass


w = WorkspaceClient()

print("="*70)
print("  LAKEBASE CONNECTION SETUP")
print("="*70)
print("\nThis script will securely store your Lakebase connection URL.")
print("\n📋 URL Format:")
print("   postgresql://username:password@host:5432/database?sslmode=require")
print("\nExample:")
print("   postgresql://myuser:mypass@ep-xxx.cloud.databricks.com:5432/databricks_postgres?sslmode=require")
print("\n" + "="*70)

# Prompt user for the full connection URL
connection_url = getpass("\n🔐 Paste your Lakebase connection URL: ")

# Validate that something was entered
if not connection_url or not connection_url.strip():
    print("\n❌ Error: No URL provided. Exiting.")
    exit(1)

connection_url = connection_url.strip()

# Basic validation
if not connection_url.startswith("postgresql://"):
    print("\n⚠️  Warning: URL should start with 'postgresql://'")
    response = input("Continue anyway? (y/n): ")
    if response.lower() != 'y':
        print("Exiting.")
        exit(1)

print("📦 Creating Databricks secret...")

# Create secret scope
try:
     w.secrets.create_scope(scope="data_base")
     print("✅ Created secret scope: data_base")
except Exception as e:
    if "already exists" in str(e).lower():
        print("✅ Secret scope 'database' already exists")
    else:
        raise

# Store the connection URL (SDK handles base64 encoding)
w.secrets.put_secret(
    scope="data_base",
    key="lakebase_url_day2",
    string_value=connection_url
)
print("✅ Stored connection URL in secrets")

# Set ACL permissions
try:
    w.secrets.put_acl(
    scope="data_base",
    principal="users",
     permission=workspace.AclPermission.READ
)
    print("✅ Set read permissions for users")
except Exception as e:
    print(f"⚠️  Could not set ACL: {e}")

print("\n" + "="*70)
print("🎉 Setup complete!")
print("="*70)
print(f"\n✅ Connection URL stored in:")
print(f"   Scope: data_base")
print(f"   Key: lakebase_url_day2")
print(f"\n✅ Permissions: All users can READ")
print(f"\n✅ Your app is ready to deploy!")
print("\n💡 Tip: You can now safely commit your code to Git.")
print("   The password is stored securely in Databricks Secrets.")
print("\n" + "="*70)


