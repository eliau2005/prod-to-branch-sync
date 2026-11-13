"""Sync engine: settings, Supabase Management API, pg_dump -> psql restore.

Port of server.mjs without the HTTP layer. Every function here blocks; the GTK
front end runs them on a worker thread. SQL text lives in syncsql.py.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import VERSION, syncsql

MGMT = "https://api.supabase.com"
APP_DIR = pathlib.Path(__file__).resolve().parent.parent
CONFIG_DIR = pathlib.Path(os.environ.get("P2B_CONFIG_DIR") or
                          pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config")
                          / "prod-branch-sync")
ENV_FILE = CONFIG_DIR / ".env"


class SyncError(Exception):
    pass


# ---------------------------------------------------------------------------
# Settings (.env, same format the Node server reads and writes)
# ---------------------------------------------------------------------------
def load_env():
    env = {}
    try:
        text = ENV_FILE.read_text()
    except OSError:
        return env
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = (x.strip() for x in line.split("=", 1))
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        env[key] = val
    return env


def persist_env_var(key, value):
    """Set KEY='value' in .env (replacing the active line, or appending); 0600, it holds prod creds."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        lines = ENV_FILE.read_text().splitlines()
    except OSError:
        lines = []
    pat = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("#") and pat.match(line):
            lines[i] = f"{key}='{value}'"
            break
    else:
        lines.append(f"{key}='{value}'")
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------
def parse_conn(url):
    u = urllib.parse.urlsplit(url)
    if not u.hostname:
        raise ValueError("no host")
    return {
        "host": u.hostname,
        "port": str(u.port or 5432),
        "user": urllib.parse.unquote(u.username or ""),
        "password": urllib.parse.unquote(u.password or ""),
        "database": (u.path or "/postgres").lstrip("/") or "postgres",
    }


def pg_env(conn):
    return {**os.environ, "PGHOST": conn["host"], "PGPORT": str(conn["port"]), "PGUSER": conn["user"],
            "PGPASSWORD": conn["password"], "PGDATABASE": conn["database"]}


_procs = set()  # running children, so abort_all() can stop a sync when the app quits


def abort_all():
    """Terminate running pg_dump/psql children. psql dying mid-restore rolls the transaction back."""
    for p in list(_procs):
        p.terminate()


def run(cmd, args, env=None, stdin=None, on_line=None, capture=False):
    """Run cmd, stream its output lines to on_line(line, is_err); return stdout if capture."""
    try:
        p = subprocess.Popen([cmd, *args], env=env, text=True, encoding="utf-8", errors="replace",
                             stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise SyncError(f'Cannot find "{cmd}". Install the PostgreSQL client tools '
                        "(sudo apt install postgresql-client) or set their folder in Settings.") from None
    _procs.add(p)
    out, err_tail = [], []

    def pump(stream, is_err):
        for line in stream:
            line = line.rstrip("\r\n")
            if capture and not is_err:
                out.append(line + "\n")
            if is_err:
                err_tail.append(line)
                del err_tail[:-30]
            if on_line and line:
                on_line(line, is_err)

    readers = [threading.Thread(target=pump, args=(p.stdout, False), daemon=True),
               threading.Thread(target=pump, args=(p.stderr, True), daemon=True)]
    for t in readers:
        t.start()
    if stdin is not None:
        p.stdin.write(stdin)
        p.stdin.close()
    code = p.wait()
    _procs.discard(p)
    for t in readers:
        t.join()
    if code != 0:
        raise SyncError(f"{os.path.basename(cmd)} exited with code {code}. " + "\n".join(err_tail).strip())
    return "".join(out)


class Engine:
    def __init__(self):
        env = load_env()
        self.token = os.environ.get("SUPABASE_ACCESS_TOKEN") or env.get("SUPABASE_ACCESS_TOKEN", "")
        self.pg_bin = os.environ.get("PG_BIN") or env.get("PG_BIN", "")
        self.prod_url = os.environ.get("PROD_DB_URL") or env.get("PROD_DB_URL", "")
        self.prod = None
        self.prod_ref = os.environ.get("SUPABASE_PROJECT_REF") or env.get("SUPABASE_PROJECT_REF", "")
        try:
            self.prod = parse_conn(self.prod_url)
            if not self.prod_ref and self.prod["user"].startswith("postgres."):
                self.prod_ref = self.prod["user"][len("postgres."):]
        except ValueError:
            pass

    # -- state ---------------------------------------------------------------
    @property
    def has_token(self):
        return bool(self.token) and self.token != "sbp_REPLACE_ME"

    def problems(self):
        p = []
        if not self.has_token:
            p.append("No access token set")
        if not self.prod_url:
            p.append("No production project selected")
        elif not self.prod:
            p.append("The production connection URL could not be parsed")
        if self.prod and not self.prod_ref:
            p.append("Could not derive the production project ref")
        return p

    def bin(self, name):
        return os.path.join(self.pg_bin, name) if self.pg_bin else name

    def psql_version(self):
        try:
            return run(self.bin("psql"), ["--version"], capture=True).strip()
        except SyncError:
            return None

    # -- Management API ------------------------------------------------------
    def mgmt(self, path, token=None):
        req = urllib.request.Request(MGMT + path, headers={
            "Authorization": f"Bearer {token or self.token}",
            "Accept": "application/json",
            "User-Agent": f"prod-branch-sync/{VERSION}",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise SyncError(f"Management API {path} -> {e.code} {e.reason} {body}") from None
        except urllib.error.URLError as e:
            raise SyncError(f"Could not reach the Supabase Management API: {e.reason}") from None

    def list_projects(self):
        """Production projects only: /v1/projects does not list preview-branch projects."""
        projects = [{"ref": p.get("ref") or p.get("id"), "name": p.get("name", ""), "region": p.get("region", ""),
                     "status": p.get("status", "")} for p in self.mgmt("/v1/projects")]
        return sorted(projects, key=lambda p: p["name"].lower())

    def list_branches(self):
        return self.mgmt(f"/v1/projects/{self.prod_ref}/branches")

    # -- settings changes ----------------------------------------------------
    def set_token(self, token):
        token = token.strip()
        if not token:
            raise SyncError("Token is empty.")
        try:
            self.mgmt("/v1/projects", token=token)
        except SyncError as e:
            raise SyncError(f"Token rejected. {e}") from None
        self.token = token
        persist_env_var("SUPABASE_ACCESS_TOKEN", token)

    def set_pg_bin(self, pg_bin):
        pg_bin = pg_bin.strip()
        version = run(os.path.join(pg_bin, "psql") if pg_bin else "psql", ["--version"], capture=True).strip()
        self.pg_bin = pg_bin
        persist_env_var("PG_BIN", pg_bin)
        return version

    def project_session_url(self, ref, password):
        """Session-pooler URL for a project; the API never returns the DB password, so the caller supplies it."""
        if not re.fullmatch(r"[a-z0-9]{20}", ref or ""):
            raise SyncError("Invalid project ref.")
        poolers = self.mgmt(f"/v1/projects/{ref}/config/database/pooler")
        poolers = poolers if isinstance(poolers, list) else []
        p = next((x for x in poolers if x.get("database_type") == "PRIMARY"), poolers[0] if poolers else None)
        if not p or not p.get("db_host") or not p.get("db_user"):
            raise SyncError("Could not read the project's pooler settings from the Management API.")
        # The API reports the transaction pooler (6543); session mode is port 5432 on the same host.
        q = urllib.parse.quote
        return f"postgresql://{q(p['db_user'], safe='')}:{q(password, safe='')}@{p['db_host']}:5432/{p.get('db_name') or 'postgres'}"

    def apply_prod_url(self, url):
        """Parse + read-only test a production URL, then make it current and persist it."""
        hint = ("Use the Session pooler string from Supabase → Connect "
                "(postgresql://postgres.<ref>:<password>@aws-…pooler.supabase.com:5432/postgres).")
        url = url.strip()
        if not url:
            raise SyncError("Connection URL is empty.")
        if not re.match(r"^postgres(ql)?://", url, re.I):
            raise SyncError(f"That is not a connection URL. {hint}")
        try:
            conn = parse_conn(url)
        except ValueError as e:
            raise SyncError(f"Could not parse the connection URL ({e}).") from None
        if re.fullmatch(r"db\.[a-z0-9]+\.supabase\.co", conn["host"], re.I):
            raise SyncError(f"That is the direct connection (IPv6-only, no project ref in the user). {hint}")
        if not conn["user"] or not conn["password"]:
            raise SyncError("The URL must include a user and a password.")
        if not conn["user"].startswith("postgres."):
            raise SyncError(f'Could not derive a project ref from user "{conn["user"]}". {hint}')
        try:
            run(self.bin("psql"), ["-v", "ON_ERROR_STOP=1", "-tA", "-c", "select 1"],
                env={**pg_env(conn), "PGCONNECT_TIMEOUT": "10"}, capture=True)
        except SyncError as e:
            if "password authentication failed" in str(e):
                raise SyncError("Wrong database password. Reset it in the dashboard (Database → Settings) "
                                "if you don't have it.") from None
            raise
        self.prod, self.prod_url = conn, url
        self.prod_ref = conn["user"][len("postgres."):]
        persist_env_var("PROD_DB_URL", url)
        return self.prod_ref

    # -- the sync ------------------------------------------------------------
    def _psql_query(self, env, sql):
        raw = run(self.bin("psql"), ["-v", "ON_ERROR_STOP=1", "-tA", "-F", "|", "-c", sql], env=env, capture=True)
        return [line.strip().split("|") for line in raw.splitlines() if line.strip()]

    def sync(self, branch_ref, log, step):
        """Copy prod into branch_ref. log(msg, is_err) and step(name) are called from this thread."""
        problems = self.problems()
        if problems:
            raise SyncError("; ".join(problems))

        log("Fetching branch list…", False)
        match = next((b for b in self.list_branches() if branch_ref in (b.get("project_ref"), b.get("id"))), None)
        if not match:
            raise SyncError(f'Branch "{branch_ref}" not found for project {self.prod_ref}.')
        if match.get("is_default"):
            raise SyncError("Refusing to target the default (main/production) branch.")
        label = match["name"] + (f" (PR #{match['pr_number']})" if match.get("pr_number") else "")
        log(f"Target: {label} — {match['project_ref']}", False)

        log("Fetching branch database credentials…", False)
        cfg = self.mgmt(f"/v1/branches/{match['project_ref']}")
        password = next((cfg[k] for k in ("db_pass", "db_password", "password")
                         if isinstance(cfg.get(k), str) and cfg[k]), None)
        if not password:
            log("Branch config keys: " + ", ".join(cfg or {}), True)
            raise SyncError("Could not find the branch DB password in the Management API response.")
        pooler_host = None
        try:
            poolers = self.mgmt(f"/v1/projects/{match['project_ref']}/config/database/pooler")
            if isinstance(poolers, list) and poolers and poolers[0].get("db_host"):
                pooler_host = poolers[0]["db_host"]
        except SyncError:
            pass  # fall back to prod's pooler host
        branch = {"host": pooler_host or self.prod["host"], "port": self.prod["port"],
                  "user": f"postgres.{match['project_ref']}", "password": password, "database": "postgres"}
        if pooler_host and pooler_host != self.prod["host"]:
            log(f"Branch pooler host: {pooler_host}", False)

        prod_env, branch_env = pg_env(self.prod), pg_env(branch)
        work = pathlib.Path(tempfile.mkdtemp(prefix="p2b-"))
        public_file, auth_file = work / "public_data.sql", work / "auth_data.sql"
        try:
            step("dump")
            log("[1/3] Dumping production: public schema…", False)
            run(self.bin("pg_dump"), ["--data-only", "--no-owner", "--no-privileges", "--schema=public",
                                      "-f", str(public_file)], env=prod_env, on_line=log)
            log("[1/3] Dumping production: auth.users + auth.identities…", False)
            run(self.bin("pg_dump"), ["--data-only", "--no-owner", "--no-privileges", "--table=auth.users",
                                      "--table=auth.identities", "-f", str(auth_file)], env=prod_env, on_line=log)
            log(f"      public: {public_file.stat().st_size} bytes · auth: {auth_file.stat().st_size} bytes", False)

            step("restore")
            log("[2/3] Checking schema drift (columns either side is missing)…", False)
            drift = syncsql.diff_schema_drift(self._psql_query(prod_env, syncsql.PROD_COLS_SQL),
                                              self._psql_query(branch_env, syncsql.BRANCH_STRICT_COLS_SQL))
            dropped = syncsql.diff_dropped_columns(self._psql_query(prod_env, syncsql.TYPED_COLS_SQL),
                                                   self._psql_query(branch_env, syncsql.TYPED_COLS_SQL))
            for t, c, *_ in drift:
                log(f"      relaxing public.{t}.{c} (NOT NULL, absent from prod) for the load", False)
            for t, c, *_ in dropped:
                log(f"      grafting public.{t}.{c} back (dropped by the branch, still in prod) so the COPY parses",
                    False)
            if not drift and not dropped:
                log("      none — the prod dump satisfies the branch schema.", False)
            files = {
                "relax_file": syncsql.drift_relax_sql(drift),
                "readd_file": syncsql.dropped_readd_sql(dropped),
                "redrop_file": syncsql.dropped_redrop_sql(dropped),
                "tighten_file": syncsql.drift_tighten_sql(drift),
            }
            psql_vars = {"auth_file": auth_file, "public_file": public_file}
            for name, sql in files.items():
                (work / f"{name}.sql").write_text(sql)
                psql_vars[name] = work / f"{name}.sql"
            # Per-user copy first; then next to the app (and the repo root, for a source checkout).
            candidates = (CONFIG_DIR / "post-load.sql", APP_DIR / "post-load.sql", APP_DIR.parent / "post-load.sql")
            post_load = next((p for p in candidates if p.exists()), None)
            if post_load:
                log(f"      {post_load} found — its backfills run after the data load.", False)
            else:
                post_load = work / "post-load-empty.sql"
                post_load.write_text("")
            psql_vars["post_load_file"] = post_load

            log(f"[2/3] Restoring into {label} (triggers + FKs disabled during load)…", False)
            args = ["-v", "ON_ERROR_STOP=1"]
            for k, v in psql_vars.items():
                args += ["-v", f"{k}={v}"]
            run(self.bin("psql"), args, env=branch_env, stdin=syncsql.RESTORE_SQL, on_line=log)

            step("verify")
            log("[3/3] Verifying row counts (prod vs branch)…", False)
            count_args = ["-tA", "-F", "|", "-c", syncsql.COUNT_SQL]
            prod_counts = syncsql.parse_counts(run(self.bin("psql"), count_args, env=prod_env, capture=True))
            branch_counts = syncsql.parse_counts(run(self.bin("psql"), count_args, env=branch_env, capture=True))
            orphans = int(run(self.bin("psql"), ["-tA", "-c", syncsql.ORPHAN_SQL], env=branch_env,
                              capture=True).strip() or 0)
            rows = [{"table": t, "prod": prod_counts.get(t), "branch": branch_counts.get(t)}
                    for t in sorted(set(prod_counts) | set(branch_counts))]
            passed = all(r["prod"] == r["branch"] for r in rows) and orphans == 0
            log("✅ Done — branch matches production." if passed else "⚠️ Done with mismatches — see table.", False)
            return {"pass": passed, "branch": label, "rows": rows, "orphans": orphans}
        finally:
            shutil.rmtree(work, ignore_errors=True)
            log("Temp dump files removed.", False)
