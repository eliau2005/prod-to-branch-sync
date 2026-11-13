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

### Schema drift shim

A branch is usually schema-**ahead** of prod: its PR migration may add a `NOT NULL` column
(without a default) to a table prod already has. The prod dump then `COPY`s without that
column, and replica mode also disables any trigger that would resolve the `NULL` — so the
load used to abort (first hit: `0112`'s `price_lists.currency`). Now, before the load, the
tool diffs `information_schema.columns` between prod and the branch and, **inside the same
restore transaction**:

1. `DROP NOT NULL` on each branch-only strict column (logged per column);
2. loads the dump;
3. runs **`post-load.sql`** (this folder, optional) — idempotent backfills for exactly those
   columns; keep every entry guarded on `information_schema` and scoped to `where <col> is null`;
4. re-adds `NOT NULL` per column. A column that still holds `NULL`s is left **nullable on the
   branch** with a `WARNING` telling you to add a backfill to `post-load.sql` — the sync
   still completes.

Branch-new tables (restored empty) and identity/generated columns are skipped — they don't
need relaxing. Once the PR merges and prod has the column, the drift disappears and the
shim is a no-op.

#### The other direction: a column the branch **dropped**

A migration that drops or renames a column creates the mirror-image problem, and it fails
harder and earlier. `pg_dump --data-only` writes an explicit column list, so
`COPY public.t (id, gone, …)` is rejected **on the statement**, before a single row is read —
an empty table fails exactly as loudly as a full one. (First hit: scandi-oms `0138`, which
replaced `conversation_proposals.kind` with `operation` and killed a sync on a table holding
zero rows in prod.)

So each prod-only column is **grafted back** onto the branch for the load — nullable and bare,
with no constraints and no default — then dropped again. Nullable and bare matters: the branch
dropped that column deliberately, and re-adding prod's constraints could fail against branch
data or, worse, succeed and leave the branch schema quietly wrong.

The order is what makes a **rename** recoverable rather than merely survivable:

1. graft the prod-only columns back;
2. load the dump;
3. run `post-load.sql` — the old and the new column are both visible *here and nowhere else*,
   which is exactly what a rename's backfill needs;
4. drop the grafted columns again, after a `WARNING` if one actually carried rows — fired on
   evidence rather than on the mere existence of drift, so it doesn't cry wolf on empty tables.

If you see that warning, prod had data in a column this branch no longer has and nothing
preserved it: add an idempotent backfill to `post-load.sql` (the `0138` entry is the worked
example) and re-run.

**Not copied:** Storage bucket file bytes (only DB rows) → bucket images 404 on the branch.
Branches are **ephemeral** — re-run after any branch reset (new push / rebase / migration).

## Requirements

- Node 18+ (tested on v22) — for the web UI.
- PostgreSQL client tools (`pg_dump`, `psql`) **17 or newer** (≥ the Supabase server version) on
  `PATH`. If they aren't, set `PG_BIN` in `.env` to the Postgres `bin` dir (e.g.
  `C:\Program Files\PostgreSQL\18\bin`, or `/usr/lib/postgresql/18/bin` on Ubuntu).
- `bash` (Git Bash on Windows) — only for the CLI fallback script.

### Ubuntu — native app

On Ubuntu the tool is a **native GTK4 / libadwaita app** (Python, `linux/prod_branch_sync/`), not
the web UI. It is styled like the Supabase dashboard (`theme.py`: same palette tokens as
`public/index.html`, dark by default, light/dark toggle in the top bar, Inter font via `fonts-inter`). Install the `.deb` from the GitHub Release, or build it with `npm run build:deb`:

```bash
sudo apt install ./prod-branch-sync_<version>_all.deb
```

It pulls PyGObject, GTK 4, libadwaita and the PostgreSQL client from the Ubuntu archive, and
adds **Prod Branch Sync** to the app menu (plus a `prod-branch-sync` command). Settings live in
`~/.config/prod-branch-sync/.env`. Put a `post-load.sql` there too if you need one.

Needs Ubuntu 24.04+ (libadwaita ≥ 1.5). Ubuntu 24.04 only ships `postgresql-client-16`, which
refuses to dump a 17 server, so add the [PGDG apt repo](https://www.postgresql.org/download/linux/ubuntu/)
first there. 25.04+ has client 17/18 in the archive.

Run it from a checkout without installing: `cd linux && python3 -m prod_branch_sync`.

The sync SQL exists twice, `lib/sync-sql.mjs` (Node) and `linux/prod_branch_sync/syncsql.py`.
`python3 test/parity.py` fails if they ever emit different SQL; the `.deb` build and CI run it.

## Setup (`.env`)

- `PROD_DB_URL` — prod **session-mode** pooler string (port 5432). Used for prod creds, the
  pooler host, and the prod project ref. You normally don't type it: in **Settings**, pick the
  production project from your account (preview branches aren't listed), enter its database
  password, and the URL is built from the project's pooler config and tested. Pasting a URL by hand
  is still available under *Enter a connection URL manually*.
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
| `server.mjs` | Zero-dependency Node web server (UI + Management API + dump/restore). Windows/macOS. |
| `lib/sync-sql.mjs` | Restore script, schema-drift shim and verification SQL (pure functions). |
| `linux/prod_branch_sync/` | Native Ubuntu app: `app.py` (GTK4/libadwaita), `engine.py`, `syncsql.py`. |
| `test/parity.py` | Fails if the Python SQL port drifts from `lib/sync-sql.mjs`. |
| `public/index.html` | The single-page UI (Tailwind via CDN). |
| `copy-prod-to-branch.sh` | CLI fallback; same restore logic, manual branch string. |
| `launcher.mjs` | Desktop entry point: starts the server, opens a browser app window, stops on close. |
| `packaging/` | Windows installer (`build.ps1`) and Ubuntu `.deb` (`build-deb.sh`) builds. |
| `post-load.sql` | Optional idempotent backfills for drifted columns in either direction (see shim above). |
| `.env` | Secrets (gitignored — `post-load.sql` is the one un-ignored `.sql`). |
