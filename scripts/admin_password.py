"""Make the ADMIN_PASSWORD_HASH value for the admin panel login.

    uv run python scripts/admin_password.py            # asks for the password (hidden)

Also prints a fresh random ADMIN_SESSION_SECRET. Paste both into .env / Render — never commit them."""

import getpass
import secrets
import sys

sys.path.insert(0, ".")
from app.core.admin_auth import hash_password  # noqa: E402

pw = getpass.getpass("Admin password: ")
if len(pw) < 8:
    sys.exit("Use at least 8 characters (12+ recommended).")
if getpass.getpass("Repeat: ") != pw:
    sys.exit("Passwords don't match.")
print(f"ADMIN_PASSWORD_HASH={hash_password(pw)}")
print(f"ADMIN_SESSION_SECRET={secrets.token_urlsafe(48)}")
