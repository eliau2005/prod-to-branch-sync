# Packaging — Windows installer & Ubuntu package

Builds a **fully self-contained** Windows installer (`.exe`) for the prod → branch
sync tool. The installer bundles:

- the **Node.js runtime** (`node.exe`) — end users don't need Node installed,
- the **PostgreSQL client tools** (`psql` / `pg_dump` + DLLs) — no separate Postgres install,
- the web UI and server.

The installed app runs locally and opens in a dedicated **Chrome app window**
(falls back to Edge, then the default browser). Closing that window stops the server.

## Build

From the repo root:

```powershell
npm run build:installer
# or, with options:
pwsh -File packaging/build.ps1                # full build
pwsh -File packaging/build.ps1 -StageOnly     # stage dist/app only (no installer)
pwsh -File packaging/build.ps1 -Version 1.2.0
```

The script:

1. Auto-detects `node.exe` and the PostgreSQL `bin` (override with `-NodeExe` / `-PgBin`).
2. Stages everything into `dist/app/`.
3. Generates `app.ico`.
4. Installs **Inno Setup** via `winget` if the compiler (`ISCC.exe`) isn't present.
5. Compiles → `dist/ProdBranchSync-Setup-<version>.exe`.

> If winget can't install Inno Setup (offline/locked-down machine), install it
> manually from <https://jrsoftware.org/isdl.php> and re-run the script.

## What the installer does

- Installs per-user to `%LOCALAPPDATA%\ProdBranchSync` (no admin; the app needs to
  write its `.env` config at runtime).
- Adds Start Menu + optional desktop shortcuts (target: `launch.vbs`, which starts
  Node hidden and opens the Chrome app window).
- Offers to launch on finish.

## First run

The app starts with no credentials. Open the **Settings** tab, set the Supabase
access token (**Create one ↗** opens your default browser, where you're signed in), then
pick the production project and enter its database password. A manual connection-URL
field is still there as a fallback. The bundled
PostgreSQL tools are used automatically — no `PG_BIN` setup needed.

## Ubuntu / Debian (.deb)

```bash
npm run build:deb                       # or: bash packaging/build-deb.sh [--version 1.2.0]
sudo apt install ./dist/prod-branch-sync_<version>_all.deb
```

The Ubuntu build is the **native GTK4 / libadwaita app** in `linux/prod_branch_sync/` — no Node,
no browser. The package is `Architecture: all` (~20 KB) and depends on Ubuntu's own
`python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1 (>= 1.5)` and `postgresql-client-17|18|19`.
The build runs `test/parity.py` first, so it needs `node` to compare against `lib/sync-sql.mjs`.

Installs the app to `/usr/lib/prod-branch-sync/`, the `/usr/bin/prod-branch-sync` launcher, a
`.desktop` entry named after the app ID (`io.github.eliau2005.ProdBranchSync`) and an SVG icon.
Per-user config: `~/.config/prod-branch-sync/` (`.env`, optional `post-load.sql`).

The release workflow builds the `.deb` on `ubuntu-latest` alongside the Windows installer and
attaches both to the GitHub Release.

## Files

| File | Role |
|------|------|
| `prod-branch-sync.iss` | Inno Setup installer definition |
| `build.ps1` | Stages the app, makes the icon, compiles the installer |
| `../launcher.mjs` | Starts the server, opens the browser app window (Windows/Linux/macOS), tracks its close |
| `build-deb.sh` | Stages the app and builds the Ubuntu/Debian `.deb` |
| `linux/` | `.deb` extras: `/usr/bin` launcher, `.desktop` entry, SVG icon |
| `../launch.vbs` | Runs the launcher with no console window (shortcut target) |
