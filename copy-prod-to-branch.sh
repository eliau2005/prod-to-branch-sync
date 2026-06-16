#!/usr/bin/env bash
#
# One-time copy of production data -> a Supabase preview branch.
#   Prod default : <your-prod-project-ref>
#
# Scope: public schema (all tables) + auth.users/auth.identities (for real logins).
# Storage file bytes are NOT copied. Preview branches are ephemeral; re-run after a reset.
#
# Setup: edit ./.env (same folder) with the prod + branch SESSION-mode pooler strings.
# Run:   bash copy-prod-to-branch.sh
#
# Both URLs MUST be the SESSION-mode pooler (port 5432), not transaction mode (6543).

set -euo pipefail

# Load connection strings from the .env sitting next to this script.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a; source "$SCRIPT_DIR/.env"; set +a
fi

: "${PROD_DB_URL:?Set PROD_DB_URL in .env (prod session-mode pooler connection string)}"
: "${BRANCH_DB_URL:?Set BRANCH_DB_URL in .env (branch session-mode pooler connection string)}"

if [[ "$PROD_DB_URL" == *"<PROD_PW>"* || "$BRANCH_DB_URL" == *"<BRANCH_PW>"* ]]; then
  echo "ERROR: .env still has a <...PW> placeholder — fill in the real password(s) first." >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT   # always shred the dumps (they contain prod PII + auth rows)

echo "==> [1/3] Dumping production data to $WORK ..."
pg_dump "$PROD_DB_URL" --data-only --no-owner --no-privileges \
  --schema=public -f "$WORK/public_data.sql"
pg_dump "$PROD_DB_URL" --data-only --no-owner --no-privileges \
  --table=auth.users --table=auth.identities -f "$WORK/auth_data.sql"

echo "    public_data.sql: $(wc -l < "$WORK/public_data.sql") lines"
echo "    auth_data.sql  : $(wc -l < "$WORK/auth_data.sql") lines"

echo "==> [2/3] Restoring into the branch (single txn, triggers + FKs disabled) ..."
psql "$BRANCH_DB_URL" -v ON_ERROR_STOP=1 \
  -v auth_file="$WORK/auth_data.sql" \
  -v public_file="$WORK/public_data.sql" <<'SQL'
begin;
set session_replication_role = replica;

-- wipe migration-seeded / leftover rows so the COPY can't collide on PKs
do $$
declare r record;
begin
  for r in select tablename from pg_tables where schemaname = 'public' loop
    execute format('truncate table public.%I cascade', r.tablename);
  end loop;
  truncate table auth.users cascade;   -- cascades to auth.identities; branch auth is empty anyway
end $$;

\i :auth_file
\i :public_file

set session_replication_role = default;
commit;
SQL

echo "==> [3/3] Verifying row counts (prod vs branch) ..."
COUNT_SQL="select 'companies' t, count(*) n from public.companies
union all select 'orders', count(*) from public.orders
union all select 'price_list_items', count(*) from public.price_list_items
union all select 'profiles', count(*) from public.profiles
union all select 'auth.users', count(*) from auth.users
order by t;"

echo "--- PROD ---"
psql "$PROD_DB_URL"   -v ON_ERROR_STOP=1 -c "$COUNT_SQL"
echo "--- BRANCH ---"
psql "$BRANCH_DB_URL" -v ON_ERROR_STOP=1 -c "$COUNT_SQL"

echo "--- BRANCH: orphan profiles (must be 0) ---"
psql "$BRANCH_DB_URL" -v ON_ERROR_STOP=1 -c \
  "select count(*) orphan_profiles from public.profiles p left join auth.users u on u.id = p.id where u.id is null;"

echo "==> Done. Dump files in $WORK removed on exit."
