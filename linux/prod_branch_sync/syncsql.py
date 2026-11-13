"""SQL for the sync: restore script, schema-drift shim, verification queries.

Line-for-line port of lib/sync-sql.mjs (read the rationale there). test/parity.py
feeds both files the same inputs and fails on any difference, so change them together.
"""

RESTORE_SQL = """begin;
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
"""

PROD_COLS_SQL = (
    "select table_name, column_name from information_schema.columns where table_schema = 'public'"
)
BRANCH_STRICT_COLS_SQL = """select table_name, column_name from information_schema.columns
    where table_schema = 'public' and is_nullable = 'NO'
      and column_default is null and is_identity = 'NO' and is_generated = 'NEVER'"""
TYPED_COLS_SQL = """select c.relname, a.attname, format_type(a.atttypid, a.atttypmod)
     from pg_attribute a
     join pg_class c on c.oid = a.attrelid
     join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'r'
      and a.attnum > 0 and not a.attisdropped"""


def diff_schema_drift(prod, strict):
    """Branch-only strict columns on tables prod also has, as [table, column]."""
    prod_tables = {r[0] for r in prod}
    prod_cols = {(r[0], r[1]) for r in prod}
    return [r for r in strict if r[0] in prod_tables and (r[0], r[1]) not in prod_cols]


def diff_dropped_columns(prod, branch):
    """Columns prod still has that the branch dropped, as [table, column, type]."""
    branch_tables = {r[0] for r in branch}
    branch_cols = {(r[0], r[1]) for r in branch}
    return [r for r in prod if r[0] in branch_tables and (r[0], r[1]) not in branch_cols]


def qi(ident):
    return '"' + ident.replace('"', '""') + '"'


def ql(s):
    return s.replace("'", "''")


def qlit(s):
    """A SQL string literal, for identifiers passed to format() at run time."""
    return "'" + ql(s) + "'"


def drift_relax_sql(drift):
    return "\n".join(
        f"alter table public.{qi(t)} alter column {qi(c)} drop not null;" for t, c, *_ in drift
    ) + "\n"


def drift_tighten_sql(drift):
    return "\n".join(f"""do $drift$
begin
  begin
    alter table public.{qi(t)} alter column {qi(c)} set not null;
  exception when others then
    raise warning 'schema-drift: public.{ql(t)}.{ql(c)} left NULLABLE on the branch (%) — add an idempotent backfill for it to post-load.sql and re-run the sync.', sqlerrm;
  end;
end $drift$;""" for t, c, *_ in drift) + "\n"


def dropped_readd_sql(dropped):
    """Add each prod-only column back, nullable and bare, so the COPY parses."""
    return "\n".join(
        f"alter table public.{qi(t)} add column if not exists {qi(c)} {ty};" for t, c, ty, *_ in dropped
    ) + "\n"


def dropped_redrop_sql(dropped):
    """Drop them again, warning first if they carried rows nothing preserved."""
    return "\n".join(f"""do $dropped$
declare n bigint;
begin
  execute format('select count(*) from public.%I where %I is not null', {qlit(t)}, {qlit(c)}) into n;
  if n > 0 then
    raise warning 'schema-drift: % prod rows carried public.{ql(t)}.{ql(c)}, which this branch has DROPPED — that data is not in the branch. If the migration renamed it, add an idempotent backfill to post-load.sql and re-run the sync.', n;
  end if;
  alter table public.{qi(t)} drop column if exists {qi(c)};
end $dropped$;""" for t, c, *_ in dropped) + "\n"


COUNT_SQL = """select 'companies' t, count(*) n from public.companies
union all select 'orders', count(*) from public.orders
union all select 'price_list_items', count(*) from public.price_list_items
union all select 'profiles', count(*) from public.profiles
union all select 'auth.users', count(*) from auth.users
order by t;"""

ORPHAN_SQL = (
    "select count(*) from public.profiles p left join auth.users u on u.id = p.id where u.id is null;"
)


def parse_counts(raw):
    """Parse `t|n` rows from psql -tA output into {table: count}."""
    m = {}
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        parts = s.split("|")
        if len(parts) >= 2:
            m[parts[0]] = int(parts[1])
    return m
