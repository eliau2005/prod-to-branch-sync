# prod → branch data sync

Copy **production** Supabase data (public schema + auth logins) into a **preview branch**.
Two ways to run it — a web UI and a CLI script. Both live in this folder and read `.env`.

> ⚠️ **Local tool only.** It holds production credentials + an account-wide Supabase access
> token and shells out to `pg_dump`/`psql`. The server binds `127.0.0.1`. **Never deploy it
> or expose the port.** `.env` and `*.sql` are gitignored.

## What it does

1. `pg_dump` production: all of `public`, plus `auth.users` + `auth.identities` (so you can
   log in with real prod accounts).
2. Restores into the target branch inside **one transaction** with
   `session_replication_role = replica` — this disables the app's insert triggers
   (notify / recompute_total / push-dispatch) **and** FK checks during load, after truncating
   the branch's existing rows so nothing collides.
3. Verifies: prints prod-vs-branch row counts for key tables + an orphan-profiles check.

**Not copied:** Storage bucket file bytes (only DB rows) → bucket images 404 on the branch.
Branches are **ephemeral** — re-run after any branch reset (new push / rebase / migration).

## Requirements

- Node 18+ (tested on v22) — for the web UI.
- PostgreSQL client tools (`pg_dump`, `psql`) on `PATH`. If they aren't, set `PG_BIN` in `.env`
  to the Postgres `bin` dir (e.g. `C:\Program Files\PostgreSQL\18\bin`).
- `bash` (Git Bash) — only for the CLI fallback script.

## Setup (`.env`)

- `PROD_DB_URL` — prod **session-mode** pooler string (port 5432). Used for prod creds, the
  pooler host, and the prod project ref.
- `SUPABASE_ACCESS_TOKEN` — create at <https://supabase.com/dashboard/account/tokens>. The web
  UI uses it to list branches and fetch each branch's DB password automatically.
- optional `PG_BIN`, `PORT` (default 8787), `SUPABASE_PROJECT_REF` (only if it can't be derived
  from `PROD_DB_URL`).

## Web UI

```bash
node server.mjs
# → http://127.0.0.1:8787
```

Pick a branch from the dropdown, click **Copy production → branch**, confirm the wipe, and watch
the live log + verification table. The production (default) branch is blocked as a target.

## CLI fallback

```bash
# set BRANCH_DB_URL in .env first, then:
bash copy-prod-to-branch.sh
```

## Files

| File | Purpose |
|---|---|
| `server.mjs` | Zero-dependency Node web server (UI + Management API + dump/restore). |
| `public/index.html` | The single-page UI (Tailwind via CDN). |
| `copy-prod-to-branch.sh` | CLI fallback; same restore logic, manual branch string. |
| `.env` | Secrets (gitignored). |
