# Deploying DataVis on Railway

This repo is **six services**, not one: `backend` (the API), `hop-web` (the operator UI),
`cube`, `litellm`, and two Postgres databases (`app-db`, `litellm-db`), plus an optional
`client-db` demo. They are defined together in `.railway/railway.ts`.

> **Do not** put a `railway.json` at the repo root. A root `railway.json` with
> `dockerfilePath: Dockerfile` makes Railway try to build the whole repo as one service from a
> Dockerfile that does not exist, so nothing runs. This repo intentionally has no root
> `railway.json` and no root `Dockerfile`.

## Why the first attempt showed nothing

1. A root `railway.json` pointed at a non-existent root `Dockerfile` → the single service had
   nothing to run.
2. After that was changed to build the `hop` folder, Hop Web was killed on startup by
   `-XX:+AggressiveHeap`, which sizes the JVM heap to the host and blows past the container
   memory limit. This repo now uses an explicit `-Xms256m -Xmx1536m` instead.
3. The `-DDATAFUSION_API_URL` / token values were empty because the referenced service was not
   named `backend`. With the IaC below, the services are named correctly and the references
   resolve.

## One-time setup (creates all six services correctly)

Needs the Railway CLI 5.42.1+, Node.js 20+, Python 3.

```bash
# If a broken single service already exists in the project, delete it first in the
# Railway dashboard (Service > Settings > Delete), so it does not clash with the IaC.

export DATAFUSION_GITHUB_REPO=praveenkaushik80/datavis
export OPENROUTER_API_KEY=sk-or-...          # first run only; omit to test with LLM_PROVIDER=fake

railway login
railway link                                 # pick the existing project (or `railway init`)
./scripts/railway-setup.sh
```

The script type-checks and applies `.railway/railway.ts`, sets any missing secrets, and creates
public domains for `backend` and `hop-web`.

## After it deploys

* **Operator UI** is the **hop-web** domain (not backend). Hop Web needs ~2 GB RAM; if it is
  killed on startup, raise it in `hop-web` → Settings → Resources.
* **API check:** open the **backend** domain at `/health` (should be `{"status":"ok"}`) and `/docs`.
  The backend has no page at `/` — that is expected.
* **hop-web must not have a healthcheck path.** Hop Web has no `/health`; a healthcheck would kill it.

## Testing without an OpenRouter key

Set `LLM_PROVIDER=fake` on the `backend` service. Research and the agent then run with a
deterministic offline model, so you can walk the whole flow before wiring up real models.

## If you must deploy the API as a single service (quick smoke test)

Set the service's **Root Directory** to `backend` (it builds `backend/Dockerfile`), add a Postgres
plugin, and set `DATABASE_URL`, `FERNET_KEY`, `JWT_SECRET`, `INTERNAL_TOKEN`, `HOP_API_TOKEN`,
`HOP_ADMIN_TOKEN`, and `LLM_PROVIDER=fake`. This gives you `/health` and `/docs` only — no Hop,
Cube, or agent tools. Use the IaC path above for the real thing.
