// SQL shared by the sync engines: the restore script, the schema-drift shim and
// verification queries. Pure functions only, so the native Ubuntu app's Python
// port (linux/prod_branch_sync/syncsql.py) can be checked against this file
// by test/parity.py.

// ---------------------------------------------------------------------------
// The restore SQL — identical logic to copy-prod-to-branch.sh
// ---------------------------------------------------------------------------
export const RESTORE_SQL = `begin;
set session_replication_role = replica;
do $$
declare r record;
begin
  for r in select tablename from pg_tables where schemaname = 'public' loop
    execute format('truncate table public.%I cascade', r.tablename);
  end loop;
  truncate table auth.users cascade;
end $$;
\\i :relax_file
\\i :readd_file
\\i :auth_file
\\i :public_file
\\i :post_load_file
\\i :redrop_file
\\i :tighten_file
set session_replication_role = default;
commit;
`;

// ---------------------------------------------------------------------------
// Schema drift shim.
//
// A branch is schema-AHEAD of prod: its migrations may add a NOT NULL column
// (no default) to a table prod already has. Prod's data-only dump then COPYs
// without that column, and replica mode keeps any NULL-resolving trigger off,
// so the load dies on the NOT NULL constraint (e.g. 0112's price_lists.currency).
//
// Fix, all inside the one restore transaction:
//   relax_file   — DROP NOT NULL on each drifted column before the load
//   post-load.sql — optional idempotent backfills maintained next to this tool
//   tighten_file — re-add NOT NULL per column; a column that still holds NULLs
//                  is left nullable with a loud WARNING instead of failing the sync
//
// Only columns on tables that EXIST in prod matter: a branch-new table is
// restored empty, so its constraints are trivially satisfied. Identity and
// generated columns self-populate under COPY and are skipped too.
//
// ── The other direction: a column the branch has DROPPED ────────────────────
//
// The case above is "the branch has a column prod lacks". The symmetric one is
// "prod has a column the branch has dropped", and it fails harder and earlier:
// `pg_dump --data-only` emits an explicit column list, so
// `COPY public.t (id, gone, ...)` is rejected on the STATEMENT, before a single
// row is read. An empty table fails exactly as loudly as a full one.
//
// This is what a rename-by-migration looks like from here — scandi-oms #339
// replaced `conversation_proposals.kind` with `operation`, and the sync died on
// a table holding zero rows in prod.
//
// Fix, mirroring the one above and inside the same transaction:
//   readd_file  — add each prod-only column back to the branch, NULLABLE and
//                 bare (no constraints, no default), purely so the COPY parses
//   post-load.sql — may now read it, which is what makes a rename backfillable
//   redrop_file — drop it again, restoring the branch's real schema, after
//                 warning if it actually carried data that nothing preserved
//
// Nullable and bare matters: the branch dropped this column deliberately, and
// re-adding it with prod's constraints could fail against branch-side data or,
// worse, succeed and leave the branch schema quietly wrong.
// ---------------------------------------------------------------------------
export const PROD_COLS_SQL =
  `select table_name, column_name from information_schema.columns where table_schema = 'public'`;
export const BRANCH_STRICT_COLS_SQL =
  `select table_name, column_name from information_schema.columns
    where table_schema = 'public' and is_nullable = 'NO'
      and column_default is null and is_identity = 'NO' and is_generated = 'NEVER'`;

/**
 * Real columns with their rendered types, for either side.
 *
 * pg_catalog rather than information_schema because `format_type` round-trips
 * exactly what `add column` needs — arrays, enums, varchar(n), numeric(p,s) —
 * where information_schema's `data_type` flattens all of those to something
 * that would not rebuild the column.
 */
export const TYPED_COLS_SQL =
  `select c.relname, a.attname, format_type(a.atttypid, a.atttypmod)
     from pg_attribute a
     join pg_class c on c.oid = a.attrelid
     join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'r'
      and a.attnum > 0 and not a.attisdropped`;

/** Branch-only strict columns on tables prod also has, as [table, column]. */
export function diffSchemaDrift(prod, strict) {
  const prodTables = new Set(prod.map(([t]) => t));
  const prodCols = new Set(prod.map(([t, c]) => `${t}|${c}`));
  return strict.filter(([t, c]) => prodTables.has(t) && !prodCols.has(`${t}|${c}`));
}

/**
 * Columns prod still has that the branch has dropped, as [table, column, type].
 *
 * Scoped to tables BOTH sides have: a table only prod has is not dumped into a
 * branch that lacks it in the first place, and a branch-only table has no prod
 * COPY to reject.
 */
export function diffDroppedColumns(prod, branch) {
  const branchTables = new Set(branch.map(([t]) => t));
  const branchCols = new Set(branch.map(([t, c]) => `${t}|${c}`));
  return prod.filter(([t, c]) => branchTables.has(t) && !branchCols.has(`${t}|${c}`));
}

const qi = (id) => '"' + id.replace(/"/g, '""') + '"';
const ql = (s) => s.replace(/'/g, "''");
/** A SQL string literal, for identifiers passed to `format()` at run time. */
const qlit = (s) => "'" + ql(s) + "'";

export const driftRelaxSql = (drift) =>
  drift.map(([t, c]) => `alter table public.${qi(t)} alter column ${qi(c)} drop not null;`).join('\n') + '\n';

export const driftTightenSql = (drift) =>
  drift.map(([t, c]) => `do $drift$
begin
  begin
    alter table public.${qi(t)} alter column ${qi(c)} set not null;
  exception when others then
    raise warning 'schema-drift: public.${ql(t)}.${ql(c)} left NULLABLE on the branch (%) — add an idempotent backfill for it to post-load.sql and re-run the sync.', sqlerrm;
  end;
end $drift$;`).join('\n') + '\n';

/** Add each prod-only column back, nullable and bare, so the COPY parses. */
export const droppedReaddSql = (dropped) =>
  dropped.map(([t, c, ty]) => `alter table public.${qi(t)} add column if not exists ${qi(c)} ${ty};`).join('\n') + '\n';

/**
 * Drop them again — but say so first if they carried anything.
 *
 * Silence here would be the dangerous outcome: a column dropped by a migration
 * that prod still fills is exactly the shape of a rename, and a rename whose
 * backfill nobody wrote leaves the branch quietly missing data it looks like it
 * has. So the warning fires on evidence (rows actually present) rather than on
 * the mere existence of the drift, which would cry wolf on every empty table.
 */
export const droppedRedropSql = (dropped) =>
  dropped.map(([t, c]) => `do $dropped$
declare n bigint;
begin
  execute format('select count(*) from public.%I where %I is not null', ${qlit(t)}, ${qlit(c)}) into n;
  if n > 0 then
    raise warning 'schema-drift: % prod rows carried public.${ql(t)}.${ql(c)}, which this branch has DROPPED — that data is not in the branch. If the migration renamed it, add an idempotent backfill to post-load.sql and re-run the sync.', n;
  end if;
  alter table public.${qi(t)} drop column if exists ${qi(c)};
end $dropped$;`).join('\n') + '\n';

export const COUNT_SQL = `select 'companies' t, count(*) n from public.companies
union all select 'orders', count(*) from public.orders
union all select 'price_list_items', count(*) from public.price_list_items
union all select 'profiles', count(*) from public.profiles
union all select 'auth.users', count(*) from auth.users
order by t;`;

export const ORPHAN_SQL =
  `select count(*) from public.profiles p left join auth.users u on u.id = p.id where u.id is null;`;

// Parse `t|n` rows from psql -tA output into { table: count }
export function parseCounts(raw) {
  const m = {};
  for (const line of raw.split(/\r?\n/)) {
    const s = line.trim();
    if (!s) continue;
    const [t, n] = s.split('|');
    if (t != null && n != null) m[t] = Number(n);
  }
  return m;
}
