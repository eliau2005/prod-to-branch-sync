-- post-load.sql — idempotent backfills for schema drift between prod and a branch.
--
-- Runs inside the restore transaction, AFTER the prod data load and BEFORE the
-- NOT NULL constraints relaxed by the drift shim are re-tightened. Both the web
-- UI and the CLI script include it automatically when it sits next to them.
--
-- Rules for every entry:
--   * Guard on information_schema — other branches may not have the migration,
--     and a reference to a missing column would abort the whole restore.
--   * Scope to NULL rows only ("where <col> is null") so the entry becomes a
--     no-op once the migration reaches prod and the dump carries real values.
--   * Entries may be deleted once their migration is live in prod; by the two
--     rules above, stale entries are harmless either way.

-- PR #342 / migration 0138: conversation_proposals.kind -> operation.
--
-- A RENAME, which is the case the graft shim exists to make backfillable: prod
-- still has `kind`, the branch has dropped it, and the shim adds it back for the
-- duration of the load so this entry can read it. Both columns are therefore
-- present right here and nowhere else — `kind` is dropped again immediately
-- after this file runs, and `operation` is re-tightened to NOT NULL after that.
--
-- A no-op in practice today: the assistant ships behind ASSISTANT_ENABLED, off
-- in production, so prod holds zero proposals. Written anyway, because "the
-- table is empty" is not a property anybody re-checks before the next sync.
do $$
begin
  if exists (select 1 from information_schema.columns
              where table_schema = 'public' and table_name = 'conversation_proposals'
                and column_name = 'operation')
     and exists (select 1 from information_schema.columns
                  where table_schema = 'public' and table_name = 'conversation_proposals'
                    and column_name = 'kind') then
    update public.conversation_proposals
       set operation = case kind
                         when 'order' then 'orders.place'
                         else 'reservations.place'
                       end
     where operation is null;
    -- 0138 derives the policy from the operation's idempotency, so a row
    -- recovered from `kind` gets the same answer the migration would give it:
    -- place_order replays a submission key and may be recovered; a reservation
    -- has no key and must never be.
    update public.conversation_proposals
       set confirmation = case kind
                            when 'order' then 'confirm'
                            else 'always-confirm'
                          end
     where kind is not null;
  end if;
end $$;

-- PR #225 / migration 0112: price_lists.currency — same backfill 0112 itself
-- runs (supplier's primary currency, defaulting to the historical EUR).
do $$
begin
  if exists (select 1 from information_schema.columns
              where table_schema = 'public' and table_name = 'price_lists'
                and column_name = 'currency') then
    update public.price_lists pl
       set currency = coalesce((select ss.currency
                                  from public.supplier_settings ss
                                 where ss.supplier_id = pl.supplier_id), 'EUR')
     where pl.currency is null;
  end if;
end $$;
