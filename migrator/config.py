import os

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "appdb")
MYSQL_URL = os.getenv("MYSQL_URL", "mysql+pymysql://app:app@localhost:3306/appdb")
APP_URL = os.getenv("APP_URL", "http://localhost:8000")

# How long the app caches the migration flags (and the demo's fault rate) before re-reading them.
SETTINGS_TTL_SECONDS = float(os.getenv("SETTINGS_TTL_SECONDS", "0.5"))

# Shadow writes to a database that isn't serving reads use clients with this timeout (seconds),
# so a hung secondary fails the shadow write fast instead of stalling the request.
SHADOW_TIMEOUT_SECONDS = float(os.getenv("SHADOW_TIMEOUT_SECONDS", "1.0"))

# Demo scaffolding, not part of the migration: lets the demo make a share of shadow writes fail on
# purpose, so the reconciler has real drift to catch. Off unless explicitly enabled (docker-compose sets
# it for the demo app only), so a stray `shadow_fault_rate` value can't break shadow writes elsewhere.
ENABLE_FAULT_INJECTION = os.getenv("ENABLE_FAULT_INJECTION", "").lower() in ("1", "true", "yes")
