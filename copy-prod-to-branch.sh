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

echo "==> [2/3] Checking schema drift (columns either side is missing) ..."
# A branch is schema-AHEAD of prod: a migration may add a NOT NULL column (no
# default) to a table prod already has. The prod dump COPYs without it, and
# replica mode disables any NULL-resolving trigger, so the load would die on
# the constraint (e.g. 0112's price_lists.currency). Relax those columns for
# the load, backfill via post-load.sql, then try to re-tighten — a column that
# still holds NULLs is left nullable with a WARNING instead of failing the sync.
# Branch-new tables restore empty (constraints trivially hold); identity and
# generated columns self-populate under COPY — both are skipped.
RELAX="$WORK/relax.sql"; TIGHTEN="$WORK/tighten.sql"
: > "$RELAX"; : > "$TIGHTEN"

psql "$PROD_DB_URL" -v ON_ERROR_STOP=1 -tA -F'|' -c \
  "select table_name, column_name from information_schema.columns where table_schema = 'public'" \
  > "$WORK/prod_cols.txt"
cut -d'|' -f1 "$WORK/prod_cols.txt" | sort -u > "$WORK/prod_tables.txt"

psql "$BRANCH_DB_URL" -v ON_ERROR_STOP=1 -tA -F'|' -c \
  "select table_name, column_name from information_schema.columns
    where table_schema = 'public' and is_nullable = 'NO'
      and column_default is null and is_identity = 'NO' and is_generated = 'NEVER'" \
| while IFS='|' read -r t c; do
    [[ -z "$t" ]] && continue
    grep -Fxq "$t" "$WORK/prod_tables.txt" || continue   # branch-new table: restored empty
    grep -Fxq "$t|$c" "$WORK/prod_cols.txt" && continue  # prod has it: dump satisfies it
    echo "    relaxing public.$t.$c (NOT NULL, absent from prod) for the load"
    printf 'alter table public."%s" alter column "%s" drop not null;\n' "$t" "$c" >> "$RELAX"
    cat >> "$TIGHTEN" <<EOF
do \$drift\$
begin
  begin
    alter table public."$t" alter column "$c" set not null;
  exception when others then
    raise warning 'schema-drift: public.$t.$c left NULLABLE on the branch (%) — add an idempotent backfill for it to post-load.sql and re-run the sync.', sqlerrm;
  end;
end \$drift\$;
EOF
  done

# The other direction: prod has a column the BRANCH has dropped. This fails
# harder and earlier — pg_dump --data-only emits an explicit column list, so
# `COPY public.t (id, gone, ...)` is rejected on the STATEMENT, before a single
# row is read, and an empty table fails exactly as loudly as a full one. That is
# what a rename-by-migration looks like from here (scandi-oms #339 replaced
# conversation_proposals.kind with operation, and the sync died on a table
# holding zero rows in prod).
#
# Graft each such column back NULLABLE and BARE so the COPY parses, let
# post-load.sql read it if a backfill wants to, then drop it again — warning
# first if it actually carried rows, because that is the case where a missing
# backfill silently loses data.
READD="$WORK/readd.sql"; REDROP="$WORK/redrop.sql"
: > "$READD"; : > "$REDROP"

# format_type round-trips what `add column` needs (arrays, enums, varchar(n)),
# where information_schema's data_type would not.
TYPED_COLS="select c.relname, a.attname, format_type(a.atttypid, a.atttypmod)
   from pg_attribute a
   join pg_class c on c.oid = a.attrelid
   join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relkind = 'r'
    and a.attnum > 0 and not a.attisdropped"

psql "$BRANCH_DB_URL" -v ON_ERROR_STOP=1 -tA -F'|' -c "$TYPED_COLS" \
  > "$WORK/branch_cols.txt"
cut -d'|' -f1 "$WORK/branch_cols.txt" | sort -u > "$WORK/branch_tables.txt"

psql "$PROD_DB_URL" -v ON_ERROR_STOP=1 -tA -F'|' -c "$TYPED_COLS" \
| while IFS='|' read -r t c ty; do
    [[ -z "$t" ]] && continue
    grep -Fxq "$t" "$WORK/branch_tables.txt" || continue   # branch lacks the table entirely
    grep -Fq "$t|$c|" "$WORK/branch_cols.txt" && continue  # branch has it: nothing to graft
    echo "    grafting public.$t.$c back (dropped by the branch, still in prod) so the COPY parses"
    printf 'alter table public."%s" add column if not exists "%s" %s;\n' "$t" "$c" "$ty" >> "$READD"
    cat >> "$REDROP" <<EOF
do \$dropped\$
declare n bigint;
begin
  execute format('select count(*) from public.%I where %I is not null', '$t', '$c') into n;
  if n > 0 then
    raise warning 'schema-drift: % prod rows carried public.$t.$c, which this branch has DROPPED — that data is not in the branch. If the migration renamed it, add an idempotent backfill to post-load.sql and re-run the sync.', n;
  end if;
  alter table public."$t" drop column if exists "$c";
end \$dropped\$;
EOF
  done

# Optional idempotent backfills maintained next to this tool.
POST_LOAD="$SCRIPT_DIR/post-load.sql"
if [[ ! -f "$POST_LOAD" ]]; then
  POST_LOAD="$WORK/post-load-empty.sql"; : > "$POST_LOAD"
fi

echo "==> [2/3] Restoring into the branch (single txn, triggers + FKs disabled) ..."
psql "$BRANCH_DB_URL" -v ON_ERROR_STOP=1 \
  -v relax_file="$RELAX" \
  -v readd_file="$READD" \
  -v auth_file="$WORK/auth_data.sql" \
  -v public_file="$WORK/public_data.sql" \
  -v post_load_file="$POST_LOAD" \
  -v redrop_file="$REDROP" \
  -v tighten_file="$TIGHTEN" <<'SQL'
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

\i :relax_file
\i :readd_file
\i :auth_file
\i :public_file
\i :post_load_file
\i :redrop_file
\i :tighten_file

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
