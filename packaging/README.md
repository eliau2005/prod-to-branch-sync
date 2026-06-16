# Packaging — Windows installer

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

The app starts with no credentials. Open the **Settings** tab to set the Supabase
access token and (in the Danger Zone) the production connection URL. The bundled
PostgreSQL tools are used automatically — no `PG_BIN` setup needed.

## Files

| File | Role |
|------|------|
| `prod-branch-sync.iss` | Inno Setup installer definition |
| `build.ps1` | Stages the app, makes the icon, compiles the installer |
| `../launcher.mjs` | Starts the server, opens the Chrome app window, tracks its close |
| `../launch.vbs` | Runs the launcher with no console window (shortcut target) |
