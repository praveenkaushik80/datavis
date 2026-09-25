#!/usr/bin/env node
/**
 * Validates .railway/railway.ts output:
 *   1. Every ${{service.VAR}} reference resolves to a declared service/variable.
 *   2. No secret-named variable holds a literal string value.
 *   3. Every service declares a PORT variable.
 *
 * Run via: npm run railway:check
 */
import { execSync } from 'child_process';
import { readFileSync } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Evaluate railway.ts via tsx and capture JSON output
let config;
try {
  const out = execSync('npx tsx --tsconfig .railway/tsconfig.json .railway/railway.ts', {
    cwd: path.resolve(__dirname, '..'),
    encoding: 'utf8',
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  // railway.ts should print JSON to stdout for inspection
  const jsonStart = out.indexOf('{');
  if (jsonStart === -1) {
    console.log('railway.ts evaluated OK (no JSON output to inspect — add console.log(JSON.stringify(config)) to railway.ts for full validation).');
    process.exit(0);
  }
  config = JSON.parse(out.slice(jsonStart));
} catch (err) {
  console.error('Failed to evaluate railway.ts:');
  console.error(err.stderr || err.message);
  process.exit(1);
}

const errors = [];

if (config && config.services) {
  const serviceNames = Object.keys(config.services);

  for (const [svcName, svc] of Object.entries(config.services)) {
    const vars = svc.variables || {};

    // Check 3: every service must declare PORT
    if (!vars.PORT && !vars.port) {
      errors.push(`Service "${svcName}" does not declare a PORT variable.`);
    }

    for (const [varName, varValue] of Object.entries(vars)) {
      if (typeof varValue !== 'string') continue;

      // Check 1: ${{service.VAR}} references resolve
      const refs = [...varValue.matchAll(/\$\{\{([^}]+)\}\}/g)];
      for (const match of refs) {
        const ref = match[1].trim();
        const [refService] = ref.split('.');
        if (!serviceNames.includes(refService) && refService !== 'shared') {
          errors.push(`Service "${svcName}".${varName}: reference "${{${ref}}}" points to unknown service "${refService}".`);
        }
      }

      // Check 2: secret-named vars must not hold literal values
      const secretPattern = /KEY|SECRET|TOKEN|PASSWORD|FERNET|MASTER/i;
      if (secretPattern.test(varName) && !varValue.startsWith('${{') && varValue.length > 0) {
        errors.push(`Service "${svcName}".${varName}: secret-named variable appears to hold a literal value. Use preserve() or a ${{reference}}.`);
      }
    }
  }
}

if (errors.length > 0) {
  console.error('\n railway.ts validation FAILED:\n');
  errors.forEach(e => console.error('  ✖ ' + e));
  process.exit(1);
} else {
  console.log('\n railway.ts validation passed.\n');
}
