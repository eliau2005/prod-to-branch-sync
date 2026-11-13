#!/usr/bin/env python3
"""Check the Python SQL port (linux/prod_branch_sync/syncsql.py) against lib/sync-sql.mjs.

Both engines must emit byte-identical SQL for the same drift. Run: python3 test/parity.py
"""
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "linux"))
from prod_branch_sync import syncsql  # noqa: E402

PROD = [["orders", "id"], ["orders", "kind"], ["price_lists", "id"], ["we\"ird", "o'col"]]
STRICT = [["orders", "id"], ["price_lists", "currency"], ["new_table", "x"], ["we\"ird", "it's"]]
PROD_TYPED = [["proposals", "id", "uuid"], ["proposals", "kind", "text[]"],
              ["proposals", "amt", "numeric(12,2)"], ["gone_table", "a", "int"], ["q\"t", "o'k", "character varying(20)"]]
BRANCH_TYPED = [["proposals", "id", "uuid"], ["proposals", "operation", "text"], ["q\"t", "id", "int"]]
COUNTS_RAW = "auth.users|12\ncompanies|3\r\n\n orders|40 \n"

js = r"""
import * as s from %s;
const i = JSON.parse(process.argv[1]);
const drift = s.diffSchemaDrift(i.prod, i.strict);
const dropped = s.diffDroppedColumns(i.prodTyped, i.branchTyped);
console.log(JSON.stringify({
  drift, dropped,
  relax: s.driftRelaxSql(drift), tighten: s.driftTightenSql(drift),
  readd: s.droppedReaddSql(dropped), redrop: s.droppedRedropSql(dropped),
  emptyRelax: s.driftRelaxSql([]), counts: s.parseCounts(i.counts),
  restore: s.RESTORE_SQL, prodCols: s.PROD_COLS_SQL, strictCols: s.BRANCH_STRICT_COLS_SQL,
  typedCols: s.TYPED_COLS_SQL, count: s.COUNT_SQL, orphan: s.ORPHAN_SQL,
}));
""" % json.dumps((ROOT / "lib" / "sync-sql.mjs").as_uri())

inp = {"prod": PROD, "strict": STRICT, "prodTyped": PROD_TYPED, "branchTyped": BRANCH_TYPED, "counts": COUNTS_RAW}
want = json.loads(subprocess.run(["node", "--input-type=module", "-e", js, json.dumps(inp)],
                                 check=True, capture_output=True, text=True).stdout)

drift = syncsql.diff_schema_drift(PROD, STRICT)
dropped = syncsql.diff_dropped_columns(PROD_TYPED, BRANCH_TYPED)
got = {
    "drift": drift, "dropped": dropped,
    "relax": syncsql.drift_relax_sql(drift), "tighten": syncsql.drift_tighten_sql(drift),
    "readd": syncsql.dropped_readd_sql(dropped), "redrop": syncsql.dropped_redrop_sql(dropped),
    "emptyRelax": syncsql.drift_relax_sql([]), "counts": syncsql.parse_counts(COUNTS_RAW),
    "restore": syncsql.RESTORE_SQL, "prodCols": syncsql.PROD_COLS_SQL, "strictCols": syncsql.BRANCH_STRICT_COLS_SQL,
    "typedCols": syncsql.TYPED_COLS_SQL, "count": syncsql.COUNT_SQL, "orphan": syncsql.ORPHAN_SQL,
}

bad = [k for k in want if want[k] != got[k]]
for k in bad:
    print(f"MISMATCH {k}:\n  js: {want[k]!r}\n  py: {got[k]!r}")
if not drift or not dropped:
    bad.append("fixtures")
    print("fixtures produced no drift; the test would prove nothing")
print("parity: FAIL" if bad else f"parity: OK ({len(want)} outputs identical)")
sys.exit(1 if bad else 0)
