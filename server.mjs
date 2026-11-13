// Local-only web UI for copying production Supabase data into a preview branch.
//
//   node server.mjs   ->   http://127.0.0.1:8787
//
// Zero dependencies (Node 18+ built-ins only). It:
//   - parses PROD_DB_URL for prod creds + pooler host + prod project ref
//   - lists branches via the Supabase Management API (needs SUPABASE_ACCESS_TOKEN)
//   - fetches the selected branch's DB password, builds its session-pooler URL
//   - runs pg_dump (prod) -> psql (branch) with the same replica-role restore as the script
//   - streams logs + a verification table to the browser over SSE
//
// SECURITY: binds 127.0.0.1 only. Holds prod creds + an account-wide token. Never deploy.

import http from 'node:http';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  RESTORE_SQL, PROD_COLS_SQL, BRANCH_STRICT_COLS_SQL, TYPED_COLS_SQL, COUNT_SQL, ORPHAN_SQL,
  diffSchemaDrift, diffDroppedColumns, driftRelaxSql, driftTightenSql, droppedReaddSql, droppedRedropSql,
  parseCounts,
} from './lib/sync-sql.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// Where .env (and an optional user post-load.sql) live. Defaults to this folder; the
// launcher points it at a per-user dir when the app is installed read-only (e.g. /opt).
const CONFIG_DIR = process.env.P2B_CONFIG_DIR || __dirname;
const HOST = '127.0.0.1';
const MGMT = 'https://api.supabase.com';

// ---------------------------------------------------------------------------
// .env loader (minimal: KEY='value' / KEY="value" / KEY=value, ignores # lines)
// ---------------------------------------------------------------------------
function loadEnv(file) {
  if (!fs.existsSync(file)) return;
  for (const raw of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq === -1) continue;
    const key = line.slice(0, eq).trim();
    let val = line.slice(eq + 1).trim();
    if ((val.startsWith("'") && val.endsWith("'")) || (val.startsWith('"') && val.endsWith('"'))) {
      val = val.slice(1, -1);
    }
    if (!(key in process.env)) process.env[key] = val;
  }
}
loadEnv(path.join(CONFIG_DIR, '.env'));

const PORT = Number(process.env.PORT) || 8787;
let TOKEN = process.env.SUPABASE_ACCESS_TOKEN || '';
let PROD_DB_URL = process.env.PROD_DB_URL || '';
let PG_BIN = process.env.PG_BIN || '';

// Persist a single KEY='value' into .env (replacing the active line, or appending).
function persistEnvVar(key, value) {
  fs.mkdirSync(CONFIG_DIR, { recursive: true });
  const file = path.join(CONFIG_DIR, '.env');
  const lines = fs.existsSync(file) ? fs.readFileSync(file, 'utf8').split(/\r?\n/) : [];
  const re = new RegExp('^\\s*' + key + '\\s*=');
  let found = false;
  for (let i = 0; i < lines.length; i++) {
    if (!lines[i].trimStart().startsWith('#') && re.test(lines[i])) {
      lines[i] = `${key}='${value}'`;
      found = true;
      break;
    }
  }
  if (!found) lines.push(`${key}='${value}'`);
  fs.writeFileSync(file, lines.join('\n'), { mode: 0o600 }); // holds prod creds
}

// ---------------------------------------------------------------------------
// Parse PROD_DB_URL -> connection parts (+ derive prod project ref & pooler host)
// ---------------------------------------------------------------------------
function parseConn(urlStr) {
  const u = new URL(urlStr);
  return {
    host: u.hostname,
    port: u.port || '5432',
    user: decodeURIComponent(u.username),
    password: decodeURIComponent(u.password),
    database: (u.pathname || '/postgres').replace(/^\//, '') || 'postgres',
  };
}
let PROD = null;
let PROD_REF = process.env.SUPABASE_PROJECT_REF || '';
try {
  PROD = parseConn(PROD_DB_URL);
  if (!PROD_REF && PROD.user.startsWith('postgres.')) PROD_REF = PROD.user.slice('postgres.'.length);
} catch {
  /* validated at startup below */
}

function pgEnv(conn) {
  return {
    ...process.env,
    PGHOST: conn.host,
    PGPORT: String(conn.port),
    PGUSER: conn.user,
    PGPASSWORD: conn.password,
    PGDATABASE: conn.database,
  };
}
const bin = (name) => (PG_BIN ? path.join(PG_BIN, name) : name);

// Build a branch's session-pooler connection using its pooler host (falling back to prod's pooler host).
function branchConn(branchRef, password, poolerHost = null) {
  return { host: poolerHost || PROD.host, port: PROD.port, user: `postgres.${branchRef}`, password, database: 'postgres' };
}

// ---------------------------------------------------------------------------
// Management API helpers
// ---------------------------------------------------------------------------
async function mgmt(pathname) {
  const res = await fetch(`${MGMT}${pathname}`, { headers: { Authorization: `Bearer ${TOKEN}` } });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`Management API ${pathname} -> ${res.status} ${res.statusText} ${body.slice(0, 300)}`);
  }
  return res.json();
}
const listBranches = () => mgmt(`/v1/projects/${PROD_REF}/branches`);
const getBranch = (branchRef) => mgmt(`/v1/branches/${branchRef}`);

// Production projects only: /v1/projects does not list preview-branch projects.
const listProjects = async () => (await mgmt('/v1/projects')).map((p) => ({
  ref: p.ref || p.id, name: p.name, region: p.region, status: p.status, organization: p.organization_slug || p.organization_id,
})).sort((a, b) => a.name.localeCompare(b.name));

// Session-pooler URL for a project. The Management API never returns the DB password,
// so the caller supplies it; host and user come from the project's pooler config.
async function projectSessionUrl(ref, password) {
  if (!/^[a-z0-9]{20}$/.test(ref)) throw new Error('Invalid project ref.');
  const poolers = await mgmt(`/v1/projects/${ref}/config/database/pooler`);
  const p = (Array.isArray(poolers) ? poolers : []).find((x) => x.database_type === 'PRIMARY') || poolers?.[0];
  if (!p?.db_host || !p?.db_user) throw new Error('Could not read the project\'s pooler settings from the Management API.');
  // The API reports the transaction pooler (6543); session mode is port 5432 on the same host.
  return `postgresql://${encodeURIComponent(p.db_user)}:${encodeURIComponent(password)}@${p.db_host}:5432/${p.db_name || 'postgres'}`;
}

// Parse + test-connect a production URL, then make it current and persist it.
async function applyProdUrl(nextUrl) {
  const poolerHint = 'Paste the full Session pooler string from Supabase → Connect '
    + '(postgresql://postgres.<ref>:<password>@aws-…pooler.supabase.com:5432/postgres).';
  if (!nextUrl) throw new Error('Connection URL is empty.');
  if (!/^postgres(ql)?:\/\//i.test(nextUrl)) throw new Error(`That is not a connection URL. ${poolerHint}`);
  let conn;
  try { conn = parseConn(nextUrl); }
  catch (e) { throw new Error(`Could not parse the connection URL. ${e.message || e}`); }
  if (/^db\.[a-z0-9]+\.supabase\.co$/i.test(conn.host)) {
    throw new Error(`That is the direct connection (IPv6-only, no project ref in the user). ${poolerHint}`);
  }
  if (!conn.host || !conn.user || !conn.password) {
    throw new Error('URL must include host, user, and password.');
  }
  const ref = conn.user.startsWith('postgres.') ? conn.user.slice('postgres.'.length) : '';
  if (!ref) throw new Error('Could not derive a project ref — use the session-pooler URL (user "postgres.<ref>").');

  // Read-only connection test (bounded so a bad host can't hang the request).
  try {
    await run(bin('psql'), ['-v', 'ON_ERROR_STOP=1', '-tA', '-c', 'select 1'],
      { env: { ...pgEnv(conn), PGCONNECT_TIMEOUT: '10' }, capture: true });
  } catch (e) {
    if (/password authentication failed/i.test(e.message)) {
      throw new Error('Wrong database password. Reset it in the dashboard under Database → Settings if you don\'t have it.');
    }
    throw e;
  }

  PROD = conn;
  PROD_REF = ref;
  PROD_DB_URL = nextUrl;
  process.env.PROD_DB_URL = nextUrl;
  persistEnvVar('PROD_DB_URL', nextUrl);
  return { ok: true, prodRef: PROD_REF, host: conn.host, user: conn.user, database: conn.database };
}

// Open a Supabase dashboard page in the system's default browser rather than the
// app window (which runs an isolated browser profile with no Supabase login).
function openExternal(target) {
  const u = new URL(target);
  if (u.protocol !== 'https:' || u.hostname !== 'supabase.com') throw new Error('Only supabase.com links can be opened.');
  const [cmd, args] = process.platform === 'win32' ? ['rundll32', ['url.dll,FileProtocolHandler', u.href]]
    : process.platform === 'darwin' ? ['open', [u.href]]
    : ['xdg-open', [u.href]];
  spawn(cmd, args, { detached: true, stdio: 'ignore', windowsHide: true }).on('error', () => {}).unref();
}

// Pick the DB password out of a branch-config payload (field name confirmed at runtime).
function pickPassword(cfg) {
  for (const k of ['db_pass', 'db_password', 'password']) {
    if (cfg && typeof cfg[k] === 'string' && cfg[k]) return cfg[k];
  }
  return null;
}

// ---------------------------------------------------------------------------
// Child-process runner: streams stdout/stderr lines to onLine, resolves on exit
// ---------------------------------------------------------------------------
function run(cmd, args, { env, stdin, onLine, capture } = {}) {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(cmd, args, { env, windowsHide: true });
    } catch (e) {
      return reject(e);
    }
    let out = '';
    let errTail = '';
    const pump = (stream, isErr) => {
      let buf = '';
      stream.setEncoding('utf8');
      stream.on('data', (d) => {
        if (capture && !isErr) out += d;
        buf += d;
        let nl;
        while ((nl = buf.indexOf('\n')) !== -1) {
          const line = buf.slice(0, nl).replace(/\r$/, '');
          buf = buf.slice(nl + 1);
          if (isErr) errTail = (errTail + '\n' + line).slice(-2000);
          if (onLine && line) onLine(line, isErr);
        }
      });
      stream.on('end', () => { if (buf && onLine) onLine(buf.replace(/\r$/, ''), isErr); });
    };
    pump(child.stdout, false);
    pump(child.stderr, true);
    child.on('error', (e) =>
      reject(e.code === 'ENOENT'
        ? new Error(`Cannot find "${cmd}". Install PostgreSQL client tools or set PG_BIN in .env. (${e.message})`)
        : e));
    child.on('close', (code) =>
      code === 0 ? resolve(out) : reject(new Error(`${path.basename(cmd)} exited with code ${code}. ${errTail.trim()}`)));
    if (stdin != null) { child.stdin.write(stdin); child.stdin.end(); }
  });
}

// ---------------------------------------------------------------------------
// psql helpers (SQL text lives in lib/sync-sql.mjs)
// ---------------------------------------------------------------------------
const psqlQuery = async (env, sql) =>
  (await run(bin('psql'), ['-v', 'ON_ERROR_STOP=1', '-tA', '-F', '|', '-c', sql], { env, capture: true }))
    .split(/\r?\n/).map((l) => l.trim()).filter(Boolean).map((l) => l.split('|'));

const computeSchemaDrift = async (prodEnv, branchEnv) =>
  diffSchemaDrift(await psqlQuery(prodEnv, PROD_COLS_SQL), await psqlQuery(branchEnv, BRANCH_STRICT_COLS_SQL));

const computeDroppedColumns = async (prodEnv, branchEnv) =>
  diffDroppedColumns(await psqlQuery(prodEnv, TYPED_COLS_SQL), await psqlQuery(branchEnv, TYPED_COLS_SQL));

// ---------------------------------------------------------------------------
// SSE sync handler
// ---------------------------------------------------------------------------
async function handleSync(branchRef, sse) {
  const send = (type, payload) => sse(type, payload);
  const log = (msg, isErr = false) => send('log', { msg, err: isErr });

  // Validate branch against the live list (and refuse the default / prod project).
  log('Fetching branch list…');
  const branches = await listBranches();
  const match = branches.find((b) => b.project_ref === branchRef || b.id === branchRef);
  if (!match) throw new Error(`Branch "${branchRef}" not found for project ${PROD_REF}.`);
  if (match.is_default) throw new Error('Refusing to target the default (main/production) branch.');
  const branchLabel = `${match.name}${match.pr_number ? ` (PR #${match.pr_number})` : ''}`;
  log(`Target: ${branchLabel} — ${match.project_ref}`);

  // Fetch branch DB password.
  log('Fetching branch database credentials…');
  const cfg = await getBranch(match.project_ref);
  const password = pickPassword(cfg);
  if (!password) {
    log(`Branch config keys: ${Object.keys(cfg || {}).join(', ')}`, true);
    throw new Error('Could not find the branch DB password in the Management API response.');
  }

  let poolerHost = null;
  try {
    const poolers = await mgmt(`/v1/projects/${match.project_ref}/config/database/pooler`);
    if (Array.isArray(poolers) && poolers.length > 0 && poolers[0].db_host) {
      poolerHost = poolers[0].db_host;
    }
  } catch {
    /* fallback to PROD.host if pooler lookup fails */
  }

  const BRANCH = branchConn(match.project_ref, password, poolerHost);
  if (poolerHost && poolerHost !== PROD.host) {
    log(`Branch pooler host: ${poolerHost}`);
  }

  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'p2b-'));
  const publicFile = path.join(work, 'public_data.sql');
  const authFile = path.join(work, 'auth_data.sql');
  const psqlPath = (p) => p.replace(/\\/g, '/'); // psql \i likes forward slashes on Windows

  try {
    // 1. Dump prod
    send('step', { step: 'dump' });
    log('[1/3] Dumping production: public schema…');
    await run(bin('pg_dump'),
      ['--data-only', '--no-owner', '--no-privileges', '--schema=public', '-f', publicFile],
      { env: pgEnv(PROD), onLine: log });
    log('[1/3] Dumping production: auth.users + auth.identities…');
    await run(bin('pg_dump'),
      ['--data-only', '--no-owner', '--no-privileges', '--table=auth.users', '--table=auth.identities', '-f', authFile],
      { env: pgEnv(PROD), onLine: log });
    log(`      public: ${fs.statSync(publicFile).size} bytes · auth: ${fs.statSync(authFile).size} bytes`);

    // 2. Restore into branch (single txn, triggers + FKs disabled)
    send('step', { step: 'restore' });
    log('[2/3] Checking schema drift (columns either side is missing)…');
    const drift = await computeSchemaDrift(pgEnv(PROD), pgEnv(BRANCH));
    const dropped = await computeDroppedColumns(pgEnv(PROD), pgEnv(BRANCH));
    if (drift.length) {
      for (const [t, c] of drift) log(`      relaxing public.${t}.${c} (NOT NULL, absent from prod) for the load`);
    }
    if (dropped.length) {
      // Named individually: this is the drift that silently discards data if a
      // rename's backfill was never written, and the operator should see which
      // columns are in play before the warnings (if any) arrive mid-load.
      for (const [t, c] of dropped) {
        log(`      grafting public.${t}.${c} back (dropped by the branch, still in prod) so the COPY parses`);
      }
    }
    if (!drift.length && !dropped.length) {
      log('      none — the prod dump satisfies the branch schema.');
    }
    const relaxFile = path.join(work, 'relax.sql');
    const tightenFile = path.join(work, 'tighten.sql');
    const readdFile = path.join(work, 'readd.sql');
    const redropFile = path.join(work, 'redrop.sql');
    fs.writeFileSync(relaxFile, driftRelaxSql(drift));
    fs.writeFileSync(tightenFile, driftTightenSql(drift));
    fs.writeFileSync(readdFile, droppedReaddSql(dropped));
    fs.writeFileSync(redropFile, droppedRedropSql(dropped));
    let postLoadFile = [CONFIG_DIR, __dirname].map((d) => path.join(d, 'post-load.sql')).find((f) => fs.existsSync(f));
    if (postLoadFile) {
      log('      post-load.sql found — its backfills run after the data load.');
    } else {
      postLoadFile = path.join(work, 'post-load-empty.sql');
      fs.writeFileSync(postLoadFile, '');
    }

    log(`[2/3] Restoring into ${branchLabel} (triggers + FKs disabled during load)…`);
    await run(bin('psql'),
      ['-v', 'ON_ERROR_STOP=1',
        '-v', `relax_file=${psqlPath(relaxFile)}`,
        '-v', `readd_file=${psqlPath(readdFile)}`,
        '-v', `auth_file=${psqlPath(authFile)}`,
        '-v', `public_file=${psqlPath(publicFile)}`,
        '-v', `post_load_file=${psqlPath(postLoadFile)}`,
        '-v', `redrop_file=${psqlPath(redropFile)}`,
        '-v', `tighten_file=${psqlPath(tightenFile)}`],
      { env: pgEnv(BRANCH), stdin: RESTORE_SQL, onLine: log });

    // 3. Verify
    send('step', { step: 'verify' });
    log('[3/3] Verifying row counts (prod vs branch)…');
    const args = ['-tA', '-F', '|', '-c', COUNT_SQL];
    const prodCounts = parseCounts(await run(bin('psql'), args, { env: pgEnv(PROD), capture: true }));
    const branchCounts = parseCounts(await run(bin('psql'), args, { env: pgEnv(BRANCH), capture: true }));
    const orphanRaw = await run(bin('psql'), ['-tA', '-c', ORPHAN_SQL], { env: pgEnv(BRANCH), capture: true });
    const orphans = Number(orphanRaw.trim());

    const tables = [...new Set([...Object.keys(prodCounts), ...Object.keys(branchCounts)])].sort();
    const rows = tables.map((t) => ({ table: t, prod: prodCounts[t] ?? null, branch: branchCounts[t] ?? null }));
    const countsMatch = rows.every((r) => r.prod === r.branch);
    const pass = countsMatch && orphans === 0;

    send('result', { pass, branch: branchLabel, rows, orphans });
    log(pass ? '✅ Done — branch matches production.' : '⚠️ Done with mismatches — see table.');
  } finally {
    fs.rmSync(work, { recursive: true, force: true });
    log('Temp dump files removed.');
  }
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------
function configError() {
  const problems = [];
  if (!TOKEN || TOKEN === 'sbp_REPLACE_ME') problems.push('SUPABASE_ACCESS_TOKEN is not set in .env');
  if (!PROD_DB_URL) problems.push('No production project selected — choose one in Settings');
  else if (!PROD) problems.push('PROD_DB_URL could not be parsed');
  if (PROD && !PROD_REF) problems.push('Could not derive prod project ref (set SUPABASE_PROJECT_REF in .env)');
  return problems;
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${HOST}:${PORT}`);

  if (url.pathname === '/' || url.pathname === '/index.html') {
    const html = fs.readFileSync(path.join(__dirname, 'public', 'index.html'));
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    return res.end(html);
  }

  if (url.pathname === '/api/config') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({
      prodRef: PROD_REF,
      hasToken: !!TOKEN && TOKEN !== 'sbp_REPLACE_ME',
      prodConfigured: !!PROD,
      prodHost: PROD ? PROD.host : '',
      prodPort: PROD ? PROD.port : '',
      prodUser: PROD ? PROD.user : '',
      prodDatabase: PROD ? PROD.database : '',
      pgBin: PG_BIN,
      platform: process.platform,
      port: PORT,
      problems: configError(),
    }));
  }

  // Set the PostgreSQL client-tools directory (PG_BIN): validate with `psql --version`, persist.
  if (url.pathname === '/api/pg-bin' && req.method === 'POST') {
    let body = '';
    req.on('data', (c) => { body += c; if (body.length > 10000) req.destroy(); });
    req.on('end', async () => {
      try {
        const pgBin = String((JSON.parse(body || '{}').pgBin || '')).trim();
        const psql = pgBin ? path.join(pgBin, 'psql') : 'psql';
        const ver = await run(psql, ['--version'], { capture: true });
        PG_BIN = pgBin;
        process.env.PG_BIN = pgBin;
        persistEnvVar('PG_BIN', pgBin);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, pgBin, version: ver.trim() }));
      } catch (e) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: String(e.message || e) }));
      }
    });
    return;
  }

  // Small JSON POST endpoints: body -> handler -> JSON (400 with { error } on failure).
  const postJson = (handler) => {
    let body = '';
    req.on('data', (c) => { body += c; if (body.length > 10000) req.destroy(); });
    req.on('end', async () => {
      try {
        const result = await handler(JSON.parse(body || '{}'));
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(result));
      } catch (e) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: String(e.message || e) }));
      }
    });
  };

  // Repoint the tool at a different production DB: parse + test-connect, then persist to .env.
  if (url.pathname === '/api/prod-url' && req.method === 'POST') {
    return postJson((b) => applyProdUrl(String(b.url || '').trim()));
  }

  // Same, but from a project picked in the list plus its DB password.
  if (url.pathname === '/api/prod-project' && req.method === 'POST') {
    return postJson(async (b) => {
      const password = String(b.password || '');
      if (!password) throw new Error('Enter the project\'s database password.');
      return applyProdUrl(await projectSessionUrl(String(b.ref || ''), password));
    });
  }

  if (url.pathname === '/api/open-external' && req.method === 'POST') {
    return postJson((b) => { openExternal(String(b.url || '')); return { ok: true }; });
  }

  if (url.pathname === '/api/projects') {
    res.setHeader('Content-Type', 'application/json');
    if (!TOKEN || TOKEN === 'sbp_REPLACE_ME') { res.writeHead(400); return res.end(JSON.stringify({ error: 'Set an access token first.' })); }
    try {
      res.writeHead(200);
      return res.end(JSON.stringify({ projects: await listProjects(), current: PROD_REF }));
    } catch (e) {
      res.writeHead(502);
      return res.end(JSON.stringify({ error: String(e.message || e) }));
    }
  }

  // Set/replace the Supabase access token from the UI: validate it, then persist to .env.
  if (url.pathname === '/api/token' && req.method === 'POST') {
    let body = '';
    req.on('data', (c) => { body += c; if (body.length > 10000) req.destroy(); });
    req.on('end', async () => {
      try {
        const token = String((JSON.parse(body || '{}').token || '')).trim();
        if (!token) throw new Error('Token is empty.');
        const check = await fetch(`${MGMT}/v1/projects`, { headers: { Authorization: `Bearer ${token}` } });
        if (!check.ok) {
          const tx = await check.text().catch(() => '');
          throw new Error(`Token rejected (${check.status} ${check.statusText}). ${tx.slice(0, 200)}`);
        }
        TOKEN = token;
        process.env.SUPABASE_ACCESS_TOKEN = token;
        persistEnvVar('SUPABASE_ACCESS_TOKEN', token);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, prodRef: PROD_REF }));
      } catch (e) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: String(e.message || e) }));
      }
    });
    return;
  }

  if (url.pathname === '/api/branches') {
    const problems = configError();
    if (problems.length) { res.writeHead(400, { 'Content-Type': 'application/json' }); return res.end(JSON.stringify({ error: problems.join('; ') })); }
    try {
      const branches = await listBranches();
      const list = branches.map((b) => ({
        name: b.name, project_ref: b.project_ref, git_branch: b.git_branch, pr_number: b.pr_number,
        persistent: b.persistent, status: b.status, is_default: b.is_default, with_data: b.with_data,
      }));
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ prodRef: PROD_REF, branches: list }));
    } catch (e) {
      res.writeHead(502, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ error: String(e.message || e) }));
    }
  }

  if (url.pathname === '/api/sync') {
    const branchRef = url.searchParams.get('branchRef') || '';
    res.writeHead(200, {
      'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive',
    });
    const sse = (type, payload) => res.write(`event: ${type}\ndata: ${JSON.stringify(payload)}\n\n`);
    const problems = configError();
    if (problems.length) { sse('fatal', { error: problems.join('; ') }); return res.end(); }
    if (!branchRef) { sse('fatal', { error: 'Missing branchRef' }); return res.end(); }
    try {
      await handleSync(branchRef, sse);
    } catch (e) {
      sse('fatal', { error: String(e.message || e) });
    } finally {
      sse('done', {});
      res.end();
    }
    return;
  }

  res.writeHead(404, { 'Content-Type': 'text/plain' });
  res.end('Not found');
});

server.listen(PORT, HOST, () => {
  const problems = configError();
  console.log(`\n  prod → branch sync UI  →  http://${HOST}:${PORT}\n`);
  console.log(`  prod project ref: ${PROD_REF || '(unknown)'}`);
  if (problems.length) {
    console.log('\n  ⚠️  Fix before use:');
    for (const p of problems) console.log(`     - ${p}`);
  }
  console.log('\n  Ctrl+C to stop.\n');
});
