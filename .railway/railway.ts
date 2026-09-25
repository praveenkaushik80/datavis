/**
 * DataVis Railway Infrastructure-as-Code definition.
 *
 * Services declared:
 *   app-db       – Railway Postgres for the DataFusion application
 *   litellm-db   – Railway Postgres for LiteLLM
 *   litellm      – LiteLLM gateway → OpenRouter   (litellm/Dockerfile)
 *   backend      – FastAPI + LangGraph agents      (backend/Dockerfile)
 *   cube         – Cube Core semantic layer        (cube/Dockerfile)
 *   hop-web      – Apache Hop Web 2.19             (hop/Dockerfile)
 *   client-db    – Optional demo shop database     (infra/client-db/Dockerfile)
 *
 * Secrets (never stored here, managed by railway-setup.sh):
 *   FERNET_KEY, JWT_SECRET, INTERNAL_TOKEN, HOP_API_TOKEN, HOP_ADMIN_TOKEN,
 *   LITELLM_MASTER_KEY, OPENROUTER_API_KEY, CUBE_API_SECRET, CLIENT_DB_PASSWORD
 *
 * Usage:
 *   npm run railway:check        # type-check + validate references
 *   railway config plan          # preview changes
 *   railway config apply         # apply to Railway project
 *
 * Notes:
 *   - preserve() means "keep the current value; only set on first deploy".
 *   - Services communicate over Railway's private network: <service>.railway.internal
 *   - Only backend and hop-web get public domains (set by railway-setup.sh).
 *   - railway.json / railway.toml are deprecated as of 2026-12-01; use this file.
 */

import { Service, Database, Variable, preserve } from 'railway';

// ---------------------------------------------------------------------------
// Databases
// ---------------------------------------------------------------------------

export const appDb = new Database({
  name: 'app-db',
  type: 'postgres',
});

export const litellmDb = new Database({
  name: 'litellm-db',
  type: 'postgres',
});

// ---------------------------------------------------------------------------
// LiteLLM  (LLM gateway → OpenRouter)
// ---------------------------------------------------------------------------

export const litellm = new Service({
  name: 'litellm',
  source: { repo: process.env.DATAFUSION_GITHUB_REPO!, branch: 'main', rootDirectory: 'litellm' },
  build: { dockerfile: 'Dockerfile' },
  deploy: {
    healthcheck: { path: '/health', timeout: 60 },
    restartPolicy: { type: 'on-failure', maxRetries: 3 },
  },
  variables: {
    PORT: '4001',
    DATABASE_URL: litellmDb.variables.DATABASE_URL,
    LITELLM_MASTER_KEY: preserve(),   // set by railway-setup.sh
    OPENROUTER_API_KEY: preserve(),   // set by railway-setup.sh
  },
});

// ---------------------------------------------------------------------------
// Backend  (FastAPI + LangGraph agents + MCP servers)
// ---------------------------------------------------------------------------

export const backend = new Service({
  name: 'backend',
  source: { repo: process.env.DATAFUSION_GITHUB_REPO!, branch: 'main', rootDirectory: 'backend' },
  build: { dockerfile: 'Dockerfile' },
  deploy: {
    healthcheck: { path: '/health', timeout: 120 },
    restartPolicy: { type: 'on-failure', maxRetries: 3 },
  },
  variables: {
    PORT: '8000',
    // App database
    DATABASE_URL: appDb.variables.DATABASE_URL,
    // LiteLLM gateway (private network)
    LLM_BASE_URL: `http://$\{\{litellm.RAILWAY_PRIVATE_DOMAIN\}\}:4001`,
    LLM_API_KEY: `$\{\{litellm.LITELLM_MASTER_KEY\}\}`,
    LLM_PROVIDER: 'litellm',
    // Cube (private network)
    CUBE_API_URL: `http://$\{\{cube.RAILWAY_PRIVATE_DOMAIN\}\}:4000/cubejs-api/v1`,
    CUBE_API_SECRET: preserve(),
    // Security
    FERNET_KEY: preserve(),
    JWT_SECRET: preserve(),
    INTERNAL_TOKEN: preserve(),
    HOP_API_TOKEN: preserve(),
    HOP_ADMIN_TOKEN: preserve(),
    // Railway-specific behaviour: stream a keepalive space every 20 s on long requests
    // so Railway's 5-minute public-request idle timeout is not triggered.
    KEEPALIVE_STREAMING: 'true',
    // Runtime
    RAILWAY: 'true',
    PYTHONUNBUFFERED: '1',
  },
});

// ---------------------------------------------------------------------------
// Cube Core  (semantic layer, no public domain by default)
// ---------------------------------------------------------------------------

export const cube = new Service({
  name: 'cube',
  source: { repo: process.env.DATAFUSION_GITHUB_REPO!, branch: 'main', rootDirectory: 'cube' },
  build: { dockerfile: 'Dockerfile' },
  deploy: {
    healthcheck: { path: '/readyz', timeout: 60 },
    restartPolicy: { type: 'on-failure', maxRetries: 3 },
  },
  variables: {
    PORT: '4000',
    // Cube fetches models and credentials from the backend at startup
    DATAFUSION_API_URL: `http://$\{\{backend.RAILWAY_PRIVATE_DOMAIN\}\}:8000`,
    INTERNAL_TOKEN: `$\{\{backend.INTERNAL_TOKEN\}\}`,
    CUBEJS_API_SECRET: preserve(),
    NODE_ENV: 'production',
  },
});

// ---------------------------------------------------------------------------
// Hop Web  (Apache Hop Web 2.19 – operator front end)
// ---------------------------------------------------------------------------

export const hopWeb = new Service({
  name: 'hop-web',
  source: { repo: process.env.DATAFUSION_GITHUB_REPO!, branch: 'main', rootDirectory: 'hop' },
  build: { dockerfile: 'Dockerfile' },
  deploy: {
    // Hop Web takes ~60 s to start the Karaf container
    healthcheck: { path: '/', timeout: 120 },
    restartPolicy: { type: 'on-failure', maxRetries: 3 },
  },
  variables: {
    PORT: '8080',
    // Passed as Java system properties through HOP_OPTIONS
    // The datafusion-railway.json environment file reads these names.
    HOP_OPTIONS: [
      '-Xmx1g',
      `-DDATAFUSION_API_URL=http://$\{\{backend.RAILWAY_PRIVATE_DOMAIN\}\}:8000`,
      `-DHOP_API_TOKEN=$\{\{backend.HOP_API_TOKEN\}\}`,
      `-DHOP_ADMIN_TOKEN=$\{\{backend.HOP_ADMIN_TOKEN\}\}`,
    ].join(' '),
  },
});

// ---------------------------------------------------------------------------
// Client-DB  (optional demo shop database; comment out if not needed)
// ---------------------------------------------------------------------------

export const clientDb = new Service({
  name: 'client-db',
  source: { repo: process.env.DATAFUSION_GITHUB_REPO!, branch: 'main', rootDirectory: 'infra/client-db' },
  build: { dockerfile: 'Dockerfile' },
  deploy: {
    healthcheck: { path: '/health', timeout: 30 },
    restartPolicy: { type: 'on-failure', maxRetries: 3 },
    // Persist data across redeploys
    volume: { mountPath: '/var/lib/postgresql/data' },
  },
  variables: {
    PORT: '5432',
    POSTGRES_DB: 'shop',
    POSTGRES_USER: 'datafusion_ro',
    POSTGRES_PASSWORD: preserve(),   // set by railway-setup.sh
  },
});
