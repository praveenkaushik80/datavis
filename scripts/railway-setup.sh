#!/usr/bin/env bash
# One-time (and safe to re-run) setup of DataFusion on Railway.
#
#   export DATAFUSION_GITHUB_REPO=your-org/datafusion-mvp     # the GitHub repo Railway builds from
#   export OPENROUTER_API_KEY=sk-or-...                        # only needed on the first run
#   railway login && railway link                              # link this folder to a Railway project
#   ./scripts/railway-setup.sh
#
# 1. checks and applies .railway/railway.ts (services, databases, volume)
# 2. sets secrets that are not set yet (never overwrites: rotating FERNET_KEY would make stored
#    database credentials unreadable). Values go through --stdin, never the command line.
# 3. creates public Railway domains for the API and Hop Web, then redeploys
set -euo pipefail
cd "$(dirname "$0")/.."

need() { command -v "$1" >/dev/null || { echo "Missing '$1'. $2"; exit 1; }; }
need railway "Install the Railway CLI (5.42.1 or newer): https://docs.railway.com/cli"
need node "Install Node.js 20+."
need python3 "Install Python 3."
: "${DATAFUSION_GITHUB_REPO:?Set DATAFUSION_GITHUB_REPO=owner/repo (the repository Railway builds from)}"

echo "==> Checking .railway/railway.ts"
[ -d node_modules/railway ] || npm install --no-audit --no-fund
npm run -s railway:check

echo "==> Plan and apply (you will be asked to confirm)"
railway config plan
railway config apply

rand() { python3 -c "import secrets; print(secrets.token_urlsafe(32))"; }
fernet() { python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; }

has_var() {   # has_var SERVICE KEY -> 0 set, 1 not set, 2 could not tell
  local out
  out=$(railway variable list --service "$1" --json 2>/dev/null) || return 2
  printf '%s' "$out" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(2)
if isinstance(data, dict):
    keys = set(data)
elif isinstance(data, list):
    keys = {v.get('name') or v.get('key') for v in data if isinstance(v, dict)}
else:
    sys.exit(2)
sys.exit(0 if sys.argv[1] in keys else 1)" "$2"
}

set_secret() {   # set_secret SERVICE KEY VALUE  (never overwrites; stops if it cannot tell)
  local rc=0
  has_var "$1" "$2" || rc=$?
  case $rc in
    0) echo "    $1.$2 already set (kept)" ;;
    1) [ -n "$3" ] || { echo "Missing value for $1.$2 (for OPENROUTER_API_KEY: export it first)."; exit 1; }
       printf '%s' "$3" | railway variable set "$2" --stdin --service "$1" --skip-deploys >/dev/null
       echo "    $1.$2 set" ;;
    *) echo "Could not read variables of service '$1' (railway variable list --json)."
       echo "Refusing to guess, so an existing $2 is never overwritten. Set it manually if it is missing."
       exit 1 ;;
  esac
}

echo "==> Secrets"
set_secret litellm OPENROUTER_API_KEY "${OPENROUTER_API_KEY:-}"
set_secret litellm LITELLM_MASTER_KEY "sk-$(rand)"
set_secret backend FERNET_KEY "$(fernet)"
for key in JWT_SECRET INTERNAL_TOKEN CUBE_API_SECRET HOP_API_TOKEN HOP_ADMIN_TOKEN; do
  set_secret backend "$key" "$(rand)"
done
if [ "${DATAFUSION_DEMO_DB:-true}" != "false" ]; then
  set_secret client-db POSTGRES_PASSWORD "$(rand)"
fi

echo "==> Public domains (API for Hop GUI and other clients, Hop Web for operators)"
railway domain --service backend || true
railway domain --service hop-web || true

echo "==> Redeploying with the new variables"
for svc in litellm backend cube hop-web; do
  railway redeploy --service "$svc" --yes || true
done
[ "${DATAFUSION_DEMO_DB:-true}" != "false" ] && { railway redeploy --service client-db --yes || true; }

cat <<'TXT'

Done. Next:
  * Hop Web: open the hop-web domain; the "datafusion" project is loaded and calls the API over the
    private network.
  * Hop GUI on your laptop: see README > "Deploy on Railway" > "Hop GUI" for HOP_OPTIONS.
  * Demo database: in 01-connect-database use DB_HOST=client-db.railway.internal, DB_PORT=5432,
    DB_DATABASE=shop, DB_USER=datafusion_ro, DB_PASSWORD=readonly-demo-password,
    DB_OPTIONS={"schemas": ["sales"]}.
  * Tokens for Hop GUI: railway variable list --service backend --kv | grep HOP_
TXT
