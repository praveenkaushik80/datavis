// Cube Core configuration for DataFusion.
//
// Cube needs no shared disk with DataFusion, so it can run anywhere (Docker Compose, Render, Railway,
// Fly.io, Kubernetes) next to a DataFusion API hosted on Vercel or in Docker:
//   * data models  -> GET /internal/cube/models        (generated when a semantic context is approved)
//   * recompile    -> GET /internal/cube/schema-version
//   * driver types -> GET /internal/cube/datasource-types
//   * credentials  -> GET /internal/cube/datasources/:name  (stored encrypted in DataFusion)
// All calls use the shared DATAFUSION_INTERNAL_TOKEN.
const API = (process.env.DATAFUSION_API_URL || 'http://backend:8000').replace(/\/$/, '');
const TOKEN = process.env.DATAFUSION_INTERNAL_TOKEN || '';

async function api(path) {
  const res = await fetch(`${API}/internal/cube${path}`, { headers: { 'X-Internal-Token': TOKEN } });
  if (!res.ok) throw new Error(`DataFusion API ${path} returned HTTP ${res.status}`);
  return res.json();
}

let typesCache = { at: 0, value: {} };
async function datasourceTypes() {
  if (Date.now() - typesCache.at > 30000) {
    typesCache = { at: Date.now(), value: await api('/datasource-types') };
  }
  return typesCache.value;
}

module.exports = {
  // Every approval bumps this value, so Cube recompiles the models on its next check.
  schemaVersion: async () => (await api('/schema-version')).version,

  repositoryFactory: () => ({
    dataSchemaFiles: async () => api('/models'),
  }),

  dbType: async ({ dataSource } = {}) => (await datasourceTypes())[dataSource] || 'postgres',

  driverFactory: async ({ dataSource }) => {
    const c = await api(`/datasources/${encodeURIComponent(dataSource)}`);
    const o = c.options || {};
    switch (c.type) {
      case 'snowflake':
        return { type: 'snowflake', account: o.account || c.host, warehouse: o.warehouse, role: o.role,
                 database: c.database, username: c.user, password: c.password };
      case 'oracle':
        return { type: 'oracle', host: c.host, port: c.port, database: o.service_name || c.database,
                 user: c.user, password: c.password };
      default:
        return { type: c.type, host: c.host, port: c.port, database: c.database, user: c.user,
                 password: c.password, ssl: o.ssl };
    }
  },

  // DataFusion's Cube MCP server signs a short-lived JWT per user (sub = email) with CUBEJS_API_SECRET
  // and already limits cubes to that user's read permissions.
  checkAuth: (req, auth) => {
    if (!auth) throw new Error('Authorization required');
  },
};
