#!/usr/bin/env bash
# Creates .env from .env.example with freshly generated secrets. Never overwrites an existing .env.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && { echo ".env already exists; not touching it."; exit 0; }
rand() { python3 -c "import secrets;print(secrets.token_urlsafe(32))"; }
fernet=$(python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
sed -e "s|^FERNET_KEY=.*|FERNET_KEY=${fernet}|" \
    -e "s|^JWT_SECRET=.*|JWT_SECRET=$(rand)|" \
    -e "s|^INTERNAL_TOKEN=.*|INTERNAL_TOKEN=$(rand)|" \
    -e "s|^CUBE_API_SECRET=.*|CUBE_API_SECRET=$(rand)|" \
    -e "s|^HOP_API_TOKEN=.*|HOP_API_TOKEN=$(rand)|" \
    -e "s|^HOP_ADMIN_TOKEN=.*|HOP_ADMIN_TOKEN=$(rand)|" \
    -e "s|^APP_DB_PASSWORD=.*|APP_DB_PASSWORD=$(rand)|" \
    -e "s|^CLIENT_DB_OWNER_PASSWORD=.*|CLIENT_DB_OWNER_PASSWORD=$(rand)|" \
    -e "s|^LITELLM_MASTER_KEY=.*|LITELLM_MASTER_KEY=sk-$(rand)|" \
    .env.example > .env
chmod 600 .env
# bind-mounted folders are written by containers running as non-root users
mkdir -p data/shared/docs hop/datafusion/review
chmod -R a+rwX data hop/datafusion 2>/dev/null || true
echo "Created .env. Now set OPENROUTER_API_KEY in it."
