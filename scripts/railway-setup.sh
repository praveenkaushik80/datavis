#!/usr/bin/env bash
# scripts/railway-setup.sh
# One-shot Railway project setup for DataVis.
#
<<<<<<< Updated upstream
# Prerequisites:
#   - Railway CLI 5.42.1+  (npm i -g @railway/cli)
#   - Node.js 20+
#   - Python 3.10+
#   - DATAFUSION_GITHUB_REPO env var set (e.g. yourorg/datavis)
#   - OPENROUTER_API_KEY env var set (first run only)
#   - `railway login` and `railway link` (or `railway init`) already done
#
# Usage:
#   export DATAFUSION_GITHUB_REPO=praveenkaushik80/datavis
#   export OPENROUTER_API_KEY=sk-or-...
=======
#   export DATAFUSION_GITHUB_REPO=praveenkaushik80/datavis     # the GitHub repo Railway builds from
#   export OPENROUTER_API_KEY=sk-or-...                        # only needed on the first run
#   railway login && railway link                              # link this folder to a Railway project
>>>>>>> Stashed changes
#   ./scripts/railway-setup.sh
#
# Re-running is safe: existing secrets are never overwritten.
set -euo pipefail

RAILWAY=${RAILWAY_CLI:-railway}

echo "==> Step 1: type-check and validate .railway/railway.ts"
npm run railway:check

echo "==> Step 2: preview the Railway configuration plan"
"$RAILWAY" config plan

read -r -p "Apply the plan? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }

echo "==> Step 3: apply the Railway configuration"
"$RAILWAY" config apply

# ---------------------------------------------------------------------------
# Helper: set a secret only if it is not already set
# ---------------------------------------------------------------------------
set_secret() {
  local service="$1" varname="$2" value="$3"

  # Check whether the variable already exists
  local existing
  existing=$("$RAILWAY" variable get "$varname" --service "$service" 2>/dev/null || true)
  if [[ -n "$existing" ]]; then
    echo "    (kept existing $service.$varname)"
    return
  fi

  echo "    setting $service.$varname"
  printf '%s' "$value" | "$RAILWAY" variable set "$varname" --service "$service" --stdin
}

echo "==> Step 4: generate and set secrets"

# Generate secrets using Python (available on all platforms)
python3 - <<'PYEOF'
import os, base64, secrets

def b64() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()

def hex32() -> str:
    return secrets.token_hex(32)

vars = {
    'FERNET_KEY':        __import__('cryptography.fernet', fromlist=['Fernet']).Fernet.generate_key().decode()
                         if True else b64(),
    'JWT_SECRET':        hex32(),
    'INTERNAL_TOKEN':    hex32(),
    'HOP_API_TOKEN':     hex32(),
    'HOP_ADMIN_TOKEN':   hex32(),
    'LITELLM_MASTER_KEY': 'sk-' + hex32(),
    'CUBE_API_SECRET':   hex32(),
    'CLIENT_DB_PASSWORD': secrets.token_urlsafe(24),
}
for k, v in vars.items():
    print(f"{k}={v}")
PYEOF
) | while IFS='=' read -r key value; do
  case "$key" in
    FERNET_KEY)         set_secret backend "$key" "$value" ;;
    JWT_SECRET)         set_secret backend "$key" "$value" ;;
    INTERNAL_TOKEN)     set_secret backend "$key" "$value"
                        set_secret cube    "CUBEJS_API_SECRET" "$value" ;;
    HOP_API_TOKEN)      set_secret backend "$key" "$value" ;;
    HOP_ADMIN_TOKEN)    set_secret backend "$key" "$value" ;;
    LITELLM_MASTER_KEY) set_secret litellm "$key" "$value" ;;
    CUBE_API_SECRET)    : ;; # handled via INTERNAL_TOKEN above
    CLIENT_DB_PASSWORD) set_secret client-db POSTGRES_PASSWORD "$value" ;;
  esac
done

# Set OPENROUTER_API_KEY (must be provided by the operator)
if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
  set_secret litellm OPENROUTER_API_KEY "$OPENROUTER_API_KEY"
else
  echo "  ⚠  OPENROUTER_API_KEY not set – set it manually:"
  echo "       railway variable set OPENROUTER_API_KEY --service litellm"
fi

echo "==> Step 5: generate public domains for backend and hop-web"
"$RAILWAY" domain --service backend   2>/dev/null || echo "  (backend domain already exists)"
"$RAILWAY" domain --service hop-web   2>/dev/null || echo "  (hop-web domain already exists)"

echo "==> Step 6: trigger redeploy"
"$RAILWAY" redeploy --service backend  --yes
"$RAILWAY" redeploy --service hop-web  --yes

echo ""
echo "✓  Setup complete."
echo "   Get service domains:  railway domain --service backend"
echo "                         railway domain --service hop-web"
echo "   Get tokens:           railway variable list --service backend --kv | grep HOP_"
