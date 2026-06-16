// launcher.mjs — desktop entry point for the bundled app.
//
// Starts the local sync server (server.mjs) and opens it in a dedicated Chrome
// "app" window (no tabs/address bar). When that window is closed, the launcher
// shuts the server down so nothing is left running.
//
// Used by the installed shortcut via launch.vbs (which runs this hidden).
//   node launcher.mjs            -> start server + open Chrome
//   node launcher.mjs --no-browser  -> start server only (for testing)

import http from 'node:http';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const noBrowser = process.argv.includes('--no-browser');
const exe = (name) => (process.platform === 'win32' ? `${name}.exe` : name);

// Prefer the bundled PostgreSQL client tools if they shipped with the app.
// This must run BEFORE importing server.mjs, which reads PG_BIN at module load.
const bundledPg = path.join(__dirname, 'pgsql', 'bin');
if (fs.existsSync(path.join(bundledPg, exe('psql')))) {
  process.env.PG_BIN = bundledPg;
}

const PORT = Number(process.env.PORT) || 8787;
const APP_URL = `http://127.0.0.1:${PORT}/`;

function ping() {
  return new Promise((resolve) => {
    const req = http.get(APP_URL, (res) => { res.resume(); resolve(true); });
    req.on('error', () => resolve(false));
    req.setTimeout(800, () => { req.destroy(); resolve(false); });
  });
}
async function waitForServer(ms = 15000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (await ping()) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

function findBrowser() {
  const pf = process.env['ProgramFiles'] || 'C:/Program Files';
  const pf86 = process.env['ProgramFiles(x86)'] || 'C:/Program Files (x86)';
  const lad = process.env['LOCALAPPDATA'] || '';
  const candidates = [
    path.join(pf, 'Google/Chrome/Application/chrome.exe'),
    path.join(pf86, 'Google/Chrome/Application/chrome.exe'),
    path.join(lad, 'Google/Chrome/Application/chrome.exe'),
    path.join(pf86, 'Microsoft/Edge/Application/msedge.exe'), // Chromium fallback (supports --app)
    path.join(pf, 'Microsoft/Edge/Application/msedge.exe'),
  ];
  return candidates.find((p) => p && fs.existsSync(p)) || null;
}

function openWindow() {
  const browser = findBrowser();
  if (!browser) {
    // Last resort: hand the URL to the default browser; can't track its lifecycle.
    spawn('cmd', ['/c', 'start', '', APP_URL], { detached: true, stdio: 'ignore' }).unref();
    return;
  }
  // Dedicated profile so --app works even if the user already has the browser open,
  // and so the spawned process maps 1:1 to the window we can watch for close.
  const profile = path.join(process.env['LOCALAPPDATA'] || __dirname, 'ProdBranchSync', 'browser-profile');
  const child = spawn(browser, [
    `--app=${APP_URL}`,
    '--window-size=1040,860',
    `--user-data-dir=${profile}`,
    '--no-first-run',
    '--no-default-browser-check',
  ], { detached: false, stdio: 'ignore' });
  // Closing the app window ends the browser process -> shut the whole app down.
  child.on('exit', () => process.exit(0));
}

async function main() {
  if (noBrowser) { await import('./server.mjs'); return; }

  // If an instance is already serving, just open another window against it.
  if (await ping()) { openWindow(); return; }

  try {
    await import('./server.mjs');
  } catch (e) {
    console.error('Failed to start server:', e?.message || e);
    process.exit(1);
  }
  const up = await waitForServer();
  if (!up) { console.error('Server did not become ready in time.'); process.exit(1); }
  openWindow();
}

main();
