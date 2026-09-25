# DataFusion MVP

Implements the DataFusion HLD and end-to-end flows:

* **Flow A (MVP1)**: connect a database, build and approve the semantic context.
* **Flow B (MVP2)**: a governed agent answers questions through auto-generated MCP servers.

| HLD component | Implemented with |
|---|---|
| Experience (operator front end) | **Apache Hop Web 2.19**: one workflow per step; the run dialog is the form, the log shows the result |
| Connection service, credential protection | FastAPI backend, SQLAlchemy connectors, Fernet encryption |
| Metadata catalog pipeline | Built-in extractor (catalogue and statistics queries only) + **OpenMetadata 2.0.2** ingestion |
| Context research agent | **LangGraph** graph: load inputs, web research (when ON), describe, relationships, knowledge, save draft |
| Draft layers, review and approval | Versioned `context_versions` (every version kept), review files, approve / reject |
| Semantic context layer, Context API | `GET /api/context/{db}`; on approval also published as **Cube Core** models (served to Cube over the API) and to OpenMetadata |
| Agent | **LangGraph** tool-calling agent |
| Auto-generated MCP servers | One MCP server per database (`app/mcp_servers/db_server.py`) + a Cube MCP server; stdio subprocesses in Docker, in-memory MCP sessions on Vercel |
| Citation and AI guardrails | sqlglot SELECT-only validation, read-only sessions, sources attached to every answer |
| Access control | Email + OTP login, JWT, RBAC by department / user to database, table, column |
| LLM gateway | **LiteLLM** (approved models, budget) forwarding to **OpenRouter**; on Vercel, OpenRouter directly (fallback models + key credit limit) |
| Observability | Telemetry table and `/api/observability/*`, surfaced through Hop workflows |

```
Hop Web ──REST──▶ DataFusion API ──▶ LangGraph agents ──MCP (stdio)──▶ db MCP server ──read-only──▶ client DB
                        │                    │                        └─ Cube MCP server ──▶ Cube Core ──▶ client DB
                        │                    └──▶ LiteLLM ──▶ OpenRouter
                        ├──▶ OpenMetadata (catalog, descriptions, glossary)
                        └──▶ app Postgres (connections, context versions, users, telemetry)
```

There are three ways to run it:

| | Docker (one host) | Railway (everything) | Vercel (API) + container host |
|---|---|---|---|
| DataFusion API + agents + MCP | `backend` container | `backend` service | **Vercel Function** (Python) |
| App database | Postgres container | Railway Postgres | **Neon Postgres** (Vercel Marketplace) |
| LLMs | LiteLLM → OpenRouter | LiteLLM service → OpenRouter | OpenRouter directly |
| Hop | Hop Web container | `hop-web` service, plus Hop GUI on laptops | Hop GUI on laptops, or Hop Web on a container host |
| Cube | container | `cube` service | container host (`deploy/remote-services.compose.yml`) |
| OpenMetadata | optional, `scripts/openmetadata.sh` | optional, its own host | optional, its own host |
| Config | `docker-compose.yml` | `.railway/railway.ts` | `backend/vercel.json` |

## Quick start (Docker)

Needs Docker with Compose v2 and an OpenRouter API key.

```bash
./scripts/init-env.sh              # creates .env with generated secrets
# edit .env: set OPENROUTER_API_KEY
docker compose up -d --build
```

| Service | URL |
|---|---|
| Hop Web | http://localhost:8080 (project `datafusion` is pre-registered) |
| API docs | http://localhost:8000/docs |
| Cube playground | http://localhost:4000 |
| LiteLLM | http://localhost:4001 |

To try it without an API key, set `LLM_PROVIDER=fake` in `.env`. An offline model then produces heuristic descriptions and simple sample-query answers.

### Optional: OpenMetadata

```bash
./scripts/openmetadata.sh up       # downloads the official 2.0.2 compose file and joins the datafusion network
```

1. Open http://localhost:8585 and sign in as `admin@open-metadata.org` / `admin`.
2. Go to Settings → Bots → ingestion-bot and copy the token.
3. In `.env`, set:
   ```
   OM_URL=http://openmetadata_server:8585
   OM_TOKEN=<the token>
   ```
4. Run `docker compose up -d backend`.

Connections registered after this are created as OpenMetadata database services, and a metadata ingestion pipeline is deployed and triggered for each one. Approved descriptions and the glossary are written back to OpenMetadata.

## Deploy on Railway

Railway runs long-lived containers, so the whole stack runs there, not just the API:

* LiteLLM, Hop Web and Cube all run as Railway services.
* The MCP servers keep their stdio subprocesses.
* Nothing is limited by a function timeout.

Services talk over Railway's private network (`<service>.railway.internal`). Only the API and Hop Web get public domains.

```
                     public                      private network (no request time limit)
Hop GUI (laptop) ──HTTPS──▶ backend ──▶ litellm ──▶ OpenRouter
Operators ─────────HTTPS──▶ hop-web ──▶ backend ──▶ cube ──▶ client databases
                                        backend ──▶ app-db (Postgres)      litellm ──▶ litellm-db
```

The project is defined in `.railway/railway.ts`, using Railway's Infrastructure as Code. `railway.json` / `railway.toml` are deprecated: new services can't use them, and they stop being read on 2026-12-01.

The definition declares:

* seven resources: two Postgres databases, `litellm`, `backend`, `cube`, `hop-web`, and an optional `client-db` demo with a volume;
* the build for each service, from its folder's Dockerfile;
* health checks;
* cross-service references, for example `DATABASE_URL` from `app-db`, and `http://${{cube.RAILWAY_PRIVATE_DOMAIN}}:4000`.

Secrets are declared with `preserve()`, so they never appear in the file. The setup script creates them.

### Setup

Needs the Railway CLI 5.42.1 or newer, Node.js 20+ and Python 3.

```bash
# 1. push this repository to GitHub, then:
export DATAFUSION_GITHUB_REPO=your-org/datafusion-mvp
export OPENROUTER_API_KEY=sk-or-...        # first run only
# export DATAFUSION_DEMO_DB=false          # skip the sample "shop" database

railway login
railway init            # or: railway link, for an existing project
./scripts/railway-setup.sh
```

The script does the following:

1. Runs `npm run railway:check`. This type-checks `.railway/railway.ts` against the Railway SDK, then evaluates it and checks that:
   * every reference resolves to a declared service or variable;
   * no secret-named variable holds a literal value;
   * every service sets `PORT`.
2. Runs `railway config plan` and `railway config apply`. You confirm the plan.
3. Sets any secret that is not set yet: `FERNET_KEY`, JWT, internal, Cube and Hop tokens, the LiteLLM master key, your OpenRouter key, and the demo DB password.
   * Values are passed through `--stdin`.
   * Existing values are never overwritten, because a new `FERNET_KEY` would make stored credentials unreadable.
   * If it can't read the current variables, it stops rather than guess.
4. Generates Railway domains for `backend` and `hop-web`, and redeploys.

It's safe to re-run. After changing `.railway/railway.ts`, run `railway config plan` then `railway config apply`.

### Using it

**Hop Web:** open the `hop-web` domain. The project is loaded and calls the API over the private network. For the demo database, set these in 01-connect-database:

| Parameter | Value |
|---|---|
| `DB_HOST` | `client-db.railway.internal` |
| `DB_PORT` | `5432` |
| `DB_DATABASE` | `shop` |
| `DB_USER` | `datafusion_ro` |
| `DB_PASSWORD` | `readonly-demo-password` |
| `DB_OPTIONS` | `{"schemas": ["sales"]}` |

**Hop GUI on a laptop** (best for workflows 06, 08 and 09, which use local files): use `hop/environments/datafusion-remote.json`, and set:

```bash
export HOP_OPTIONS="-Xmx2g -DDATAFUSION_API_URL=https://<backend-domain> -DHOP_API_TOKEN=... -DHOP_ADMIN_TOKEN=..."
```

Get the tokens with `railway variable list --service backend --kv | grep HOP_`.

**Cube playground:** Cube has no public domain by default. Add one with `railway domain --service cube` if you want it.

### Railway limits to know about

* **Public requests close after 5 minutes without data.** They may run up to 15 minutes while data flows; the private network has no limit. So on Railway, `KEEPALIVE_STREAMING=true`, and long calls (context research, agent questions) stream a space every 20 s before the JSON.
  * Because the HTTP status is sent first, a failure during the work returns `{"detail": ..., "datafusion_error": true}` in the body.
  * All Hop pipelines treat that marker as a failure.
  * Other API clients should check for it too.
* **Uploads** must finish within 5 minutes.
* **Hop Web review files** (`/project/review`) live in the container and are lost on redeploy. Do the review round trip (08 → edit → 09) from Hop GUI, or attach a volume to `hop-web`.
* **Networking:**
  * Services bind `::` (IPv4 and IPv6). The backend's `start.sh` falls back to `0.0.0.0` on hosts without IPv6.
  * Client databases outside Railway must accept connections from Railway. Use a read-only account and TLS, or Railway's static outbound IPs (paid plans) for allow-listing.
* **OpenMetadata** can run on Railway in principle, but it needs Elasticsearch, Airflow and several GB of memory. Run it on its own host and set `OM_URL` / `OM_TOKEN` on `backend` with `railway variable set`.

## Deploy on Vercel

Vercel runs the DataFusion API: both LangGraph agents and the auto-generated MCP servers, in process. The pieces that are long-running servers run elsewhere and talk to the API over HTTPS, so nothing needs a shared disk:

* **Hop Web, Cube Core and OpenMetadata** stay off Vercel. Vercel Functions cannot run a JVM web app, a Cube server or Elasticsearch.
* **LiteLLM** is replaced by calling OpenRouter directly.

```
Hop (laptop or container host) ──HTTPS──▶ Vercel: DataFusion API ──▶ Neon Postgres
Cube (container host) ◀──models, credentials── /internal/cube/*  ──▶ OpenRouter
                                             └──read-only──▶ your client databases
```

### 1. Create the Vercel project

1. Push this repository to GitHub and import it in Vercel.
2. Set **Root Directory** to `backend`. The Python preset detects FastAPI from `requirements.txt` and loads `app` from `backend/index.py`. `vercel.json` sets the function timeout and excludes tests from the bundle, which comes to about 230 MB of the 500 MB limit.
3. Go to **Storage → Neon Postgres → Connect**. This adds `DATABASE_URL`, and tables are created on first request. Prefer the pooled (`-pooler`) connection string: the API opens a fresh connection per request on serverless.
4. Add the environment variables listed in `deploy/vercel.env.example`. Generate the secrets with `./scripts/init-env.sh`, which writes them into `.env`, then copy them across. Also set:

   | Variable | Value |
   |---|---|
   | `LLM_BASE_URL` | `https://openrouter.ai/api/v1` |
   | `LLM_API_KEY` | Your OpenRouter key. Set a credit limit on that key in OpenRouter; it replaces LiteLLM's budget. |
   | `LLM_FALLBACK_MODELS` | OpenRouter tries these models if the primary one fails |

5. Deploy, then open `https://<project>.vercel.app/health` and `/docs`.

The CLI alternative: `cd backend && vercel deploy`.

### 2. Run Cube and Hop Web next to it

Cube pulls its data models, driver types and credentials from the API (`/internal/cube/*`, protected by `INTERNAL_TOKEN`). It can run on any container host:

```bash
cp deploy/remote.env.example deploy/remote.env      # same tokens as in Vercel
docker compose -f deploy/remote-services.compose.yml --env-file deploy/remote.env up -d
```

Then set `CUBE_API_URL=https://<cube-host>/cubejs-api/v1` in Vercel and redeploy. `cube/Dockerfile` builds the same Cube image for platforms that deploy from a Dockerfile (Render, Railway, Fly.io).

### 3. Point Hop at the Vercel API

**Hop Web:** the compose file above already does this.

**Hop GUI on a laptop:**

1. Open `hop/datafusion` as a project.
2. Add an environment that uses `hop/environments/datafusion-remote.json`.
3. Start Hop with the URL and tokens as Java system properties:

   ```bash
   export HOP_OPTIONS="-Xmx2g -DDATAFUSION_API_URL=https://<project>.vercel.app -DHOP_API_TOKEN=... -DHOP_ADMIN_TOKEN=..."
   ./hop-gui.sh
   ```

Every workflow works unchanged. Documents and review files travel over HTTPS:

* Workflow 06 uploads a local file.
* Workflow 08 saves the review document to `hop/datafusion/review/`.
* Workflow 09 sends your edited copy back.

### Vercel limits to know about

* **Duration.** Functions run for up to 300 s on Hobby and up to 800 s on Pro (raise `maxDuration` in `backend/vercel.json`). Context research on a large database can exceed 300 s. Use Pro, or research a database in smaller schemas (`DB_OPTIONS={"schemas": [...]}`).
* **Request size.** Request bodies are limited to 4.5 MB, so larger documents must be split.
* **Network.** Your client databases must accept connections from Vercel, since the MCP servers query them from the function. Vercel egress IPs are not fixed on Hobby and Pro, so use TLS, a read-only account, and an allow-list or private networking option on your side (Vercel Secure Compute on Enterprise).
* **Not available on Vercel:**
  * Docling. It is too large, so PDF and Word are parsed with pypdf and python-docx.
  * The shared-folder endpoints (`documents/from-path`, `edits/from-review-file`).
  * The stdio MCP command for external clients.

## Using Apache Hop as the front end

In Hop Web, open a workflow under `workflows/` and click Run. Fill in the parameters, then read the API response in the log. A failed call marks the run as failed and shows the API's error message.

| Workflow | Step |
|---|---|
| `00-check-api` | Check Hop can reach the API |
| `flow-a/01-connect-database` | Steps 1–5: choose DB, credentials, verify, store encrypted, run catalog |
| `flow-a/02-rerun-metadata-catalog` | Re-run catalog / OpenMetadata ingestion |
| `flow-a/03-browse-tables`, `04-table-column-page` | Step 6–7: explore tables and columns |
| `flow-a/05-add-business-meaning` | Step 7: edit descriptions, add business meaning |
| `flow-a/06-upload-document` | Step 7: upload a local PDF / Word / JSON / TXT / CSV (sent over HTTPS) |
| `flow-a/07-run-context-research` | Step 8–9: research agent creates a draft (`WEB_SEARCH=true` to allow web search) |
| `flow-a/08-review-draft` | Step 10: saves the editable review document to `hop/datafusion/review/<db>-context-<v>.json` |
| `flow-a/09-apply-review-edits` | Step 10: sends your edited copy of that file back |
| `flow-a/10-approve-context` / `11-reject-context` | Step 11: approve (publishes Context API, Cube model, OpenMetadata) or reject |
| `flow-a/12-list-versions` | Version history |
| `flow-b/20-ask-question` | Ask the agent (testing Flow B from Hop) |
| `flow-b/21-context-api` | See the approved context |
| `admin/30-add-user`, `31-grant-read-access`, `32-list-permissions` | Access control (uses the admin token) |
| `observability/40-usage-summary`, `41-recent-events` | Usage, tokens, tools, databases, errors |

### Demo with the sample database

The `client-db` service contains a `shop` database with a `sales` schema. It has customers, products, orders and order items, plus a read-only `datafusion_ro` account. Run these workflows in order:

1. **01-connect-database**, with these parameters:

   | Parameter | Value |
   |---|---|
   | `DB_NAME` | `shop` |
   | `DB_TYPE` | `postgres` |
   | `DB_HOST` | `client-db` |
   | `DB_PORT` | `5432` |
   | `DB_DATABASE` | `shop` |
   | `DB_USER` | `datafusion_ro` |
   | `DB_PASSWORD` | `readonly-demo-password` |
   | `DB_OPTIONS` | `{"schemas": ["sales"]}` |

2. **06-upload-document** with `FILE=/shared/docs/shop/business-rules.txt` (the path inside the Hop Web container).
3. **07-run-context-research**, then **08-review-draft**. Edit `hop/datafusion/review/shop-context-latest.json` on your machine, then run **09-apply-review-edits**.
4. **10-approve-context** with the version number from step 3.
5. **30-add-user** and **31-grant-read-access**, then **20-ask-question**.

### Hop tips

* Tokens reach Hop as Java system properties (`HOP_OPTIONS` in `docker-compose.yml`). Hop does not expand OS environment variables in environment files.
* Parameter values are pasted into a JSON body. Avoid double quotes in free-text parameters.
* Hop falls back to a parameter's default when you leave it empty on the command line.
* The DB password is visible in the run dialog while you type it. It is removed from the row before anything is logged, and it is stored only encrypted.
* The Hop read timeout is 15 minutes in Docker and 320 s in the remote environment, just above Vercel's 300 s limit.

## Models (OpenRouter)

All model calls go to LiteLLM (`litellm/config.yaml`), which forwards them to OpenRouter. The backend only knows three aliases:

| Alias | OpenRouter model |
|---|---|
| `datafusion-agent` | `anthropic/claude-sonnet-5` |
| `datafusion-research` | `anthropic/claude-sonnet-5` |
| `datafusion-fallback` | `anthropic/claude-haiku-4.5` |

* **Changing models:** edit the config file; no code changes are needed. The agent needs a model that supports tool calling.
* **Budget:** LiteLLM enforces a spend cap (`max_budget`).
* **Skipping LiteLLM:** see the comment at the top of `.env.example`.

## Using the MCP servers from other clients

The per-database servers are ordinary MCP servers (stdio). For example, in Claude Desktop:

```json
{"mcpServers": {"datafusion-shop": {
  "command": "docker",
  "args": ["compose", "-f", "/path/to/datafusion-mvp/docker-compose.yml", "exec", "-T", "backend",
           "python", "-m", "app.mcp_servers.db_server", "--connection", "shop", "--user", "ana@example.com"]}}}
```

Tools:

* `list_tables`
* `describe_table`
* `search_context`
* `run_readonly_query`
* `query_<table>` (one per table, generated from the approved context)

The server only exposes what that user may read.

## Security model

* **Credentials:**
  * Fernet-encrypted in the app database.
  * Masked in API responses.
  * Scrubbed from error messages.
  * Never included in prompts.
  * Removed from Hop rows before logging.
  * Cube fetches them, and the generated models, at runtime from internal endpoints protected by `INTERNAL_TOKEN`.
  * Note: OpenMetadata also stores the credentials it needs for ingestion, in its own secrets store.
* **Read-only, in three layers:**
  1. Use a read-only database account (recommended; the sample `datafusion_ro` is one).
  2. Sessions are read-only at the database level where supported.
  3. Every query is validated by sqlglot. Only a single SELECT is allowed, on permitted tables and columns, with blocked functions and a row limit.
* **Research agent:** web search is off by default. When on, only business terms derived from table and column names are sent, never data.
* **Tokens:** Hop uses a steward token, and a separate admin token for access-control workflows. People sign in with email + OTP.

## Development

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest -q
```

The tests run Flow A and Flow B end to end twice:

* **Docker mode:** stdio MCP servers and the shared folder.
* **Vercel mode:** `VERCEL=1`, in-memory MCP, and files over HTTP.

Both use SQLite as the client database and the offline model. They also cover guardrails, permissions, OTP, the OpenRouter client configuration and the Neon URL handling.

To regenerate the Hop files after editing `hop/generate_hop_files.py`, run `python hop/generate_hop_files.py`.

## What has and has not been verified

**Verified:**

* The backend test suite (19 tests), including Vercel mode and keep-alive streaming.
* `.railway/railway.ts`:
  * type-checked against the Railway SDK 3.11.0;
  * evaluated locally, with its references and secret handling checked;
  * the checker itself tested with deliberately broken input.
* `scripts/railway-setup.sh`, exercised against a stub `railway` CLI: existing secrets kept, missing ones set, unreadable output stops the script.
* Hop against a backend in Railway mode, covering keep-alive success and an error reported after HTTP 200.
* Every Hop workflow, run with the Apache Hop 2.19 runtime (`hop-run`), against the API in both Docker mode and Vercel mode. This includes:
  * binary Word upload over HTTP;
  * the review-file round trip;
  * Flow B through in-process MCP;
  * secret redaction in Hop logs.
* `cube.js`, run in Node against the API: models, schema version, driver type and credentials.
* OpenMetadata endpoints and connection shapes, against the 2.0.2 schemas.
* The Vercel bundle size, measured by installing `requirements.txt` into a clean folder (about 230 MB).
* Image tags.

**Not verified here** (no Docker or Vercel account in the build environment):

* An actual `vercel deploy` and a Neon database.
* An actual `railway config apply` and Railway deploy. The plan/apply engine runs inside the Railway CLI and needs an account. Two behaviours are taken from Railway's documentation, not observed:
  * `${{service.VAR}}` references inside literal values are resolved at deploy time;
  * `preserve()` on a variable that does not exist yet is a no-op.
* The compose stacks as a whole.
* Live PostgreSQL, OpenMetadata ingestion, Cube queries, LiteLLM and OpenRouter calls.
* The MVP2 drivers.

Expect some first-run adjustments on these; the Vercel function logs and `docker compose logs` will show where.

## Known limitations

* Hop is an operator console, not an end-user chat interface. A small chat page on `/api/ask` would be the natural next front end for business users.
* Research runs synchronously inside one request. On Vercel that caps it at the function's max duration; moving it to Vercel Queues or Workflows is the next step if you need longer runs.
* No RAG in MVP1: documents are passed straight to the agent (about 12k characters per call).
* The Future Enhancements from the HLD are not started: sessions, memory, schema-change refresh and data freshness.
