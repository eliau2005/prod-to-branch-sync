"""GTK4 / libadwaita window for Prod Branch Sync, styled like the Supabase dashboard.

Layout mirrors the web UI (public/index.html): breadcrumb top bar, underlined tabs,
flat bordered cards. Colors and component styles live in theme.py. Blocking engine
calls run on worker threads; results return to the UI thread through GLib.idle_add.
Targets libadwaita >= 1.5 (Ubuntu 24.04).
"""
import pathlib
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from . import APP_ID, VERSION, theme  # noqa: E402
from .engine import CONFIG_DIR, Engine, abort_all  # noqa: E402

TOKENS_URL = "https://supabase.com/dashboard/account/tokens"
STEPS = [("dump", "Dump"), ("restore", "Restore"), ("verify", "Verify")]
CONTENT_WIDTH = 768  # the web UI's max-w-3xl
HOME_CONFIG = str(CONFIG_DIR / ".env").replace(str(pathlib.Path.home()), "~", 1)


def background(work, done):
    """Run work() on a thread; call done(result, error) on the UI thread."""
    def target():
        try:
            result, error = work(), None
        except Exception as e:  # noqa: BLE001 - surfaced to the user verbatim
            result, error = None, e
        GLib.idle_add(lambda: done(result, error) and False)
    threading.Thread(target=target, daemon=True).start()


# ---------------------------------------------------------------------------
# Small widget builders
# ---------------------------------------------------------------------------
def label(text="", classes=(), **kw):
    kw.setdefault("xalign", 0)
    kw.setdefault("wrap", True)
    return Gtk.Label(label=text, css_classes=list(classes), **kw)


def vbox(spacing=0, classes=(), **kw):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, css_classes=list(classes), **kw)


def hbox(spacing=0, classes=(), **kw):
    return Gtk.Box(spacing=spacing, css_classes=list(classes), **kw)


def button(text, kind, on_click, **kw):
    b = Gtk.Button(label=text, css_classes=[f"sb-{kind}"], valign=Gtk.Align.CENTER, **kw)
    b.connect("clicked", lambda *_: on_click())
    return b


def card(title=None, desc=None, suffix=None, danger=False, padded=True):
    """A dashboard card; returns (card, body). body is padded unless padded=False (for row lists)."""
    c = vbox(classes=["sb-card"] + (["danger"] if danger else []))
    if title:
        head = hbox(12, ["sb-card-head"])
        text = vbox(2, hexpand=True)
        text.append(label(title, ["sb-card-title"]))
        if desc:
            text.append(label(desc, ["sb-card-desc"]))
        head.append(text)
        if suffix:
            suffix.set_valign(Gtk.Align.CENTER)
            head.append(suffix)
        c.append(head)
    body = vbox(10, ["sb-card-body"] if padded else [])
    c.append(body)
    return c, body


def dropdown(items):
    d = Gtk.DropDown(model=Gtk.StringList.new(items), hexpand=True)
    d.add_css_class("sb-select")
    return d


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Prod Branch Sync", default_width=880, default_height=900)
        self.engine = Engine()
        self.branches, self.projects = [], []
        self.syncing = False

        self.theme_name = theme.saved_theme()
        self.css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(self.get_display(), self.css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.apply_theme()

        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack(vhomogeneous=False)
        # Settings first: building it creates widgets refresh_overview() also updates.
        settings = self.page(self.build_settings_page())
        self.stack.add_titled(self.page(self.build_sync_page()), "sync", "Overview")
        self.stack.add_titled(settings, "settings", "Settings")
        self.toasts.set_child(self.stack)

        view = Adw.ToolbarView()
        view.add_top_bar(self.build_header())
        view.add_top_bar(self.build_tabs())
        view.set_content(self.toasts)
        self.set_content(view)
        self.connect("close-request", self.on_close_request)

        self.refresh_overview()
        self.load_branches()
        if self.engine.has_token:
            self.load_projects()
        else:
            self.stack.set_visible_child_name("settings")
            self.set_status(self.token_status, "No access token set yet — paste one to load projects and branches.",
                            "warn")

    # -- chrome --------------------------------------------------------------
    def apply_theme(self):
        dark = self.theme_name == "dark"
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT)
        css = theme.css(self.theme_name)
        if hasattr(self.css, "load_from_string"):
            self.css.load_from_string(css)
        else:
            self.css.load_from_data(css, -1)

    def toggle_theme(self):
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        theme.save_theme(self.theme_name)
        self.apply_theme()
        self.sync_theme_icon()

    def sync_theme_icon(self):
        # The icon for the theme you'd switch TO, like the web UI: sun while dark, moon while light.
        self.theme_btn.set_icon_name("weather-clear-symbolic" if self.theme_name == "dark"
                                     else "weather-clear-night-symbolic")

    def build_header(self):
        header = Adw.HeaderBar(show_title=False)
        crumbs = hbox(10, margin_start=8)
        tex = theme.logo_texture()
        if tex:
            crumbs.append(Gtk.Image(paintable=tex, pixel_size=22))
        crumbs.append(label("Supabase", ["sb-crumb-app"], wrap=False))
        crumbs.append(label("/", ["sb-crumb-sep"], wrap=False))
        crumbs.append(label("Branch data sync", ["sb-crumb-page"], wrap=False))
        header.pack_start(crumbs)

        menu = Gio.Menu()
        menu.append("Open settings folder", "win.open-config")
        menu.append("About Prod Branch Sync", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu",
                                       css_classes=["flat"]))
        self.theme_btn = Gtk.Button(tooltip_text="Toggle theme", css_classes=["sb-icon-btn"],
                                    valign=Gtk.Align.CENTER)
        self.theme_btn.connect("clicked", lambda *_: self.toggle_theme())
        self.sync_theme_icon()
        header.pack_end(self.theme_btn)
        self.add_action_cb("open-config", lambda *_: self.open_uri(CONFIG_DIR.as_uri(), mkdir=True))
        self.add_action_cb("about", lambda *_: self.show_about())
        return header

    def build_tabs(self):
        bar = hbox(classes=["sb-tabs"])
        bar.append(Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE, halign=Gtk.Align.START))
        return bar

    @staticmethod
    def page(content):
        clamp = Adw.Clamp(maximum_size=CONTENT_WIDTH, child=content, margin_top=32, margin_bottom=32,
                          margin_start=20, margin_end=20)
        return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)

    @staticmethod
    def heading(box, title, lead):
        h = vbox(4, margin_bottom=4)
        h.append(label(title, ["sb-h1"]))
        h.append(label(lead, ["sb-lead"]))
        box.append(h)

    # -- helpers -------------------------------------------------------------
    def add_action_cb(self, name, cb):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", cb)
        self.add_action(action)

    def open_uri(self, uri, mkdir=False):
        """Open in the system's default handler (the default browser for web links)."""
        if mkdir:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        Gtk.UriLauncher.new(uri).launch(self, None, None, None)

    @staticmethod
    def set_status(lbl, msg, kind="info"):
        for c in ("sb-ok", "sb-bad", "dim-label"):
            lbl.remove_css_class(c)
        lbl.add_css_class({"ok": "sb-ok", "err": "sb-bad", "warn": "sb-bad"}.get(kind, "dim-label"))
        lbl.set_label(msg)
        lbl.set_visible(bool(msg))

    def confirm(self, heading, body, action_label, on_yes):
        d = Adw.AlertDialog(heading=heading, body=body, body_use_markup=True)
        d.add_response("cancel", "Cancel")
        d.add_response("go", action_label)
        d.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("cancel")
        d.set_close_response("cancel")
        d.connect("response", lambda _d, r: r == "go" and on_yes())
        d.present(self)

    # -- Overview page -------------------------------------------------------
    def build_sync_page(self):
        box = vbox(20)
        self.heading(box, "Prod → Branch data sync",
                     "Copy production data (public schema + auth logins) into a Supabase preview branch.")

        self.problems = hbox(12, ["sb-callout", "err"], visible=False)
        self.problems_label = label("", hexpand=True)
        self.problems.append(self.problems_label)
        self.problems.append(button("Open Settings", "secondary",
                                    lambda: self.stack.set_visible_child_name("settings")))
        box.append(self.problems)

        self.ov_pill = label("—", ["sb-pill"], wrap=False)
        prod, body = card("Production", suffix=self.ov_pill, padded=False)
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.ov = {}
        for key, title in [("name", "Project"), ("ref", "Project ref"), ("host", "Pooler host"),
                           ("user", "Database user"), ("db", "Database"), ("token", "Access token"),
                           ("psql", "psql tools"), ("branches", "Preview branches")]:
            row = hbox(16, margin_start=20, margin_end=20, margin_top=9, margin_bottom=9)
            row.append(label(title, ["sb-dt"], width_chars=18, max_width_chars=18, wrap=False))
            value = label("—", ["sb-dd"] + ([] if key in ("name", "token", "branches") else ["mono"]),
                          selectable=True, hexpand=True, wrap_mode=Pango.WrapMode.CHAR)
            row.append(value)
            self.ov[key] = value
            rows.append(Gtk.ListBoxRow(child=row, activatable=False))
        body.append(rows)
        box.append(prod)

        box.append(label("⚠️ Local tool. Reads production read-only and wipes the selected branch. "
                         "Storage file bytes are not copied; branches are ephemeral.",
                         ["sb-callout", "warn"]))

        target, body = card()
        body.append(label("Target preview branch", ["sb-card-title"]))
        controls = hbox(10)
        self.branch_dd = dropdown(["Loading branches…"])
        self.branch_dd.set_sensitive(False)
        self.branch_dd.connect("notify::selected", lambda *_: self.sync_run_button())
        controls.append(self.branch_dd)
        self.refresh_btn = button("↻", "secondary", self.load_branches, tooltip_text="Refresh branches")
        controls.append(self.refresh_btn)
        self.run_btn = button("Copy production → branch", "primary", self.on_run_clicked, sensitive=False)
        controls.append(self.run_btn)
        body.append(controls)
        self.prodref_label = label("", ["sb-card-desc"])
        body.append(self.prodref_label)
        box.append(target)

        # Progress: steps, verdict, counts table, log (hidden until a sync starts)
        self.progress = vbox(12, visible=False)
        steps = hbox(8)
        self.step_labels = {}
        for i, (key, name) in enumerate(STEPS):
            if i:
                steps.append(label("→", ["sb-arrow"], wrap=False))
            self.step_labels[key] = label(f"{i + 1} · {name}", ["sb-step"], wrap=False)
            steps.append(self.step_labels[key])
        self.spinner = Gtk.Spinner(margin_start=6)
        steps.append(self.spinner)
        self.progress.append(steps)

        self.verdict = label("", ["sb-callout"], visible=False)
        self.progress.append(self.verdict)
        self.counts_card, self.counts_body = card()
        self.counts_card.set_visible(False)
        self.progress.append(self.counts_card)

        self.log_buf = Gtk.TextBuffer()
        self.err_tag = self.log_buf.create_tag("err", foreground="#f59e0b")
        log_view = Gtk.TextView(buffer=self.log_buf, editable=False, cursor_visible=False, monospace=True,
                                wrap_mode=Gtk.WrapMode.WORD_CHAR, css_classes=["sb-log", "mono"])
        self.log_scroller = Gtk.ScrolledWindow(child=log_view, min_content_height=320,
                                               css_classes=["sb-log-frame"])
        self.progress.append(self.log_scroller)
        box.append(self.progress)

        box.append(label(f"Local tool · credentials stay in {HOME_CONFIG}", ["sb-footer"],
                         margin_top=8))
        return box

    def refresh_overview(self):
        e = self.engine
        problems = e.problems()
        self.problems_label.set_label("Setup needed: " + "; ".join(problems) if problems else "")
        self.problems.set_visible(bool(problems))
        proj = next((p for p in self.projects if p["ref"] == e.prod_ref), None)
        dash = "—"
        self.ov["name"].set_label(proj["name"] if proj else dash)
        self.ov["ref"].set_label(e.prod_ref or dash)
        self.ov["host"].set_label(f"{e.prod['host']}:{e.prod['port']}" if e.prod else dash)
        self.ov["user"].set_label(e.prod["user"] if e.prod else dash)
        self.ov["db"].set_label(e.prod["database"] if e.prod else dash)
        self.ov["token"].set_markup('<span foreground="#3ECF8E">●</span> Connected' if e.has_token
                                    else '<span foreground="#ef4444">●</span> Not set')
        ready = not problems
        self.ov_pill.set_label("Ready" if ready else ("Token required" if not e.has_token else "Setup needed"))
        for c in ("ok", "bad"):
            self.ov_pill.remove_css_class(c)
        self.ov_pill.add_css_class("ok" if ready else "bad")
        self.prodref_label.set_label(f"Production project: {e.prod_ref}" if e.prod_ref else "")
        self.prod_current.set_label(f"{e.prod['user']} @ {e.prod['host']} / {e.prod['database']}  ·  ref {e.prod_ref}"
                                    if e.prod else "Not configured")
        self.token_entry.set_property("placeholder-text", "Saved — paste a new token to replace it" if e.has_token else "sbp_…")
        background(e.psql_version, lambda v, _err: self.ov["psql"].set_label(
            (e.pg_bin or "system PATH") + (f"  ·  {v.replace('psql (PostgreSQL) ', 'v')}" if v
                                           else "  ·  not found — sudo apt install postgresql-client")))

    def load_branches(self):
        if not self.engine.has_token or not self.engine.prod_ref:
            self.set_branch_items([], "Set the token and production project in Settings")
            return
        self.set_branch_items([], "Loading branches…")

        def done(branches, err):
            if err:
                self.set_branch_items([], "Failed to load")
                self.problems_label.set_label(str(err))
                self.problems.set_visible(True)
                return
            self.branches = [b for b in branches if not b.get("is_default")]
            self.set_branch_items(self.branches, "No preview branches found", loaded=True)
        background(self.engine.list_branches, done)

    def set_branch_items(self, branches, empty_label, loaded=False):
        if branches:
            labels = []
            for b in branches:
                bits = [b["name"]]
                if b.get("pr_number"):
                    bits.append(f"PR #{b['pr_number']}")
                bits.append(b.get("status", ""))
                if b.get("persistent"):
                    bits.append("persistent")
                labels.append(" · ".join(x for x in bits if x))
        else:
            labels = [empty_label]
        self.branch_dd.set_model(Gtk.StringList.new(labels))
        self.branch_dd.set_sensitive(bool(branches) and not self.syncing)
        n = len(branches)
        self.ov["branches"].set_label((f"{n} branch" + ("" if n == 1 else "es")) if loaded else "—")
        self.sync_run_button()

    def selected_branch(self):
        i = self.branch_dd.get_selected()
        return self.branches[i] if self.branches and 0 <= i < len(self.branches) else None

    def sync_run_button(self):
        self.run_btn.set_sensitive(bool(self.selected_branch()) and not self.syncing and not self.engine.problems())

    def on_run_clicked(self):
        b = self.selected_branch()
        if not b:
            return
        name = GLib.markup_escape_text(b["name"])
        self.confirm("Confirm destructive copy",
                     f"This will <b>permanently wipe all data</b> in <b>{name}</b> and replace it with a copy of "
                     "<b>production</b> (public schema + auth logins). Production is only read. Continue?",
                     "Wipe & copy", lambda: self.start_sync(b))

    def start_sync(self, branch):
        self.syncing = True
        self.branch_dd.set_sensitive(False)
        self.refresh_btn.set_sensitive(False)
        self.sync_run_button()
        self.progress.set_visible(True)
        self.verdict.set_visible(False)
        self.counts_card.set_visible(False)
        self.log_buf.set_text("")
        for key, _ in STEPS:
            self.set_step(key, "")
        self.spinner.start()

        def log(msg, is_err):
            GLib.idle_add(self.append_log, msg, is_err)

        def step(name):
            GLib.idle_add(self.on_step, name)

        background(lambda: self.engine.sync(branch["project_ref"], log, step), self.on_sync_done)

    def set_step(self, key, state):
        lbl = self.step_labels[key]
        for c in ("active", "done"):
            lbl.remove_css_class(c)
        if state:
            lbl.add_css_class(state)

    def on_step(self, name):
        order = [k for k, _ in STEPS]
        for k in order[:order.index(name)]:
            self.set_step(k, "done")
        self.set_step(name, "active")
        return False

    def append_log(self, msg, is_err):
        end = self.log_buf.get_end_iter()
        if is_err:
            self.log_buf.insert_with_tags(end, msg + "\n", self.err_tag)
        else:
            self.log_buf.insert(end, msg + "\n")
        adj = self.log_scroller.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper()) and False)
        return False

    def show_verdict(self, text, kind):
        for c in ("ok", "err", "warn"):
            self.verdict.remove_css_class(c)
        self.verdict.add_css_class(kind)
        self.verdict.set_label(text)
        self.verdict.set_visible(True)

    def on_sync_done(self, result, err):
        self.syncing = False
        self.spinner.stop()
        self.branch_dd.set_sensitive(bool(self.branches))
        self.refresh_btn.set_sensitive(True)
        self.sync_run_button()
        if err:
            self.append_log(f"FATAL: {err}", True)
            self.show_verdict("⚠️ Sync failed — see the log below.", "err")
            return
        for key, _ in STEPS:
            self.set_step(key, "done")
        if result["pass"]:
            self.show_verdict(f"✅ PASS — {result['branch']} matches production "
                              f"(orphan profiles: {result['orphans']})", "ok")
        else:
            self.show_verdict(f"⚠️ MISMATCH — review the table (orphan profiles: {result['orphans']})", "err")

        while (child := self.counts_body.get_first_child()) is not None:
            self.counts_body.remove(child)
        grid = Gtk.Grid(column_spacing=24, row_spacing=8)
        for col, (text, xalign) in enumerate([("TABLE", 0), ("PROD", 1), ("BRANCH", 1), ("OK", 0)]):
            grid.attach(label(text, ["sb-card-desc"], xalign=xalign, hexpand=col == 0, wrap=False), col, 0, 1, 1)
        for i, r in enumerate(result["rows"], start=1):
            ok = r["prod"] == r["branch"]
            cls = [] if ok else ["sb-bad"]
            grid.attach(label(r["table"], ["mono"] + cls, wrap=False), 0, i, 1, 1)
            grid.attach(label(str(r["prod"] if r["prod"] is not None else "—"), cls, xalign=1, wrap=False), 1, i, 1, 1)
            grid.attach(label(str(r["branch"] if r["branch"] is not None else "—"), cls, xalign=1, wrap=False),
                        2, i, 1, 1)
            grid.attach(label("✓" if ok else "✗", ["sb-ok" if ok else "sb-bad"], wrap=False), 3, i, 1, 1)
        self.counts_body.append(grid)
        self.counts_card.set_visible(True)

    # -- Settings page -------------------------------------------------------
    def build_settings_page(self):
        box = vbox(20)
        self.heading(box, "Settings", "Manage the access token, production connection, and client tools. "
                                      f"Saved locally to {HOME_CONFIG}.")

        # Access token
        tok, body = card()
        top = hbox()
        top.append(label("Supabase access token", ["sb-card-title"], hexpand=True))
        top.append(button("Create one ↗", "link", lambda: self.open_uri(TOKENS_URL),
                          tooltip_text="Opens supabase.com in your default browser"))
        body.append(top)
        body.append(label("Account-wide token, used to list projects and branches and to fetch branch DB passwords.",
                          ["sb-card-desc"]))
        row = hbox(10)
        self.token_entry = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True, placeholder_text="sbp_…")
        self.token_entry.connect("activate", lambda *_: self.on_token_save())
        row.append(self.token_entry)
        self.token_btn = button("Save token", "primary", self.on_token_save)
        row.append(self.token_btn)
        body.append(row)
        self.token_status = label("", ["sb-card-desc"], visible=False)
        body.append(self.token_status)
        box.append(tok)

        # PostgreSQL client tools
        pg, body = card()
        body.append(label("PostgreSQL client tools", ["sb-card-title"]))
        body.append(label("Directory containing psql / pg_dump (version 17 or newer). Leave blank to use the "
                          "system PATH. Verified with psql --version before saving.", ["sb-card-desc"]))
        row = hbox(10)
        self.pg_entry = Gtk.Entry(hexpand=True, text=self.engine.pg_bin,
                                  placeholder_text="(system PATH)  ·  e.g. /usr/lib/postgresql/18/bin")
        self.pg_entry.add_css_class("mono")
        self.pg_entry.connect("activate", lambda *_: self.on_pg_save())
        row.append(self.pg_entry)
        row.append(button("Save & test", "secondary", self.on_pg_save))
        body.append(row)
        self.pg_status = label("", ["sb-card-desc"], visible=False)
        body.append(self.pg_status)
        box.append(pg)

        # Danger Zone: production connection
        dz, body = card("Danger Zone — production connection",
                        "Repoint this tool at a different production database.", danger=True)
        body.append(label("Current production target", ["sb-card-desc"]))
        self.prod_current = label("—", ["sb-dd", "mono"], selectable=True, wrap_mode=Pango.WrapMode.CHAR)
        body.append(self.prod_current)

        body.append(label("Production project", ["sb-card-title"], margin_top=8))
        prow = hbox(10)
        self.project_dd = dropdown(["Set an access token to list projects"])
        self.project_dd.set_sensitive(False)
        prow.append(self.project_dd)
        prow.append(button("↻", "secondary", self.load_projects, tooltip_text="Reload projects"))
        body.append(prow)
        body.append(label("Production projects on your account (preview branches are not listed).",
                          ["sb-card-desc"]))

        pw_head = hbox(margin_top=8)
        pw_head.append(label("Database password", ["sb-card-title"], hexpand=True))
        pw_head.append(button("Forgot it? Reset in dashboard ↗", "link", self.open_project_db_settings))
        body.append(pw_head)
        self.project_pw = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        self.project_pw.connect("activate", lambda *_: self.on_project_apply())
        body.append(self.project_pw)
        body.append(label("Supabase never returns it through the API. The session-pooler URL is built for you "
                          "and tested read-only before saving.", ["sb-card-desc"]))
        arow = hbox(12)
        self.use_project_btn = button("Use this project", "danger", self.on_project_apply)
        arow.append(self.use_project_btn)
        self.project_status = label("", ["sb-card-desc"], visible=False, hexpand=True)
        arow.append(self.project_status)
        body.append(arow)

        body.append(Gtk.Separator(margin_top=8, margin_bottom=4))
        manual = vbox(10, margin_top=8)
        self.url_entry = Gtk.PasswordEntry(
            show_peek_icon=True, hexpand=True,
            placeholder_text="postgresql://postgres.<ref>:<password>@<pooler-host>:5432/postgres")
        manual.append(self.url_entry)
        manual.append(label("Use the session-pooler URL (user postgres.<ref>). Tested read-only before saving.",
                            ["sb-card-desc"]))
        mrow = hbox(12)
        self.url_btn = button("Update production connection", "danger", self.on_url_apply)
        mrow.append(self.url_btn)
        self.url_status = label("", ["sb-card-desc"], visible=False, hexpand=True)
        mrow.append(self.url_status)
        manual.append(mrow)
        body.append(Gtk.Expander(label="Enter a connection URL manually", child=manual))
        box.append(dz)

        box.append(label(f"Local tool · credentials stay in {HOME_CONFIG}", ["sb-footer"],
                         margin_top=8))
        return box

    def on_token_save(self):
        token = self.token_entry.get_text().strip()
        if not token:
            self.set_status(self.token_status, "Enter a token first.", "warn")
            return
        self.token_btn.set_sensitive(False)
        self.set_status(self.token_status, "Validating…")

        def done(_r, err):
            self.token_btn.set_sensitive(True)
            if err:
                self.set_status(self.token_status, str(err), "err")
                return
            self.token_entry.set_text("")
            self.set_status(self.token_status, "Saved ✓", "ok")
            self.refresh_overview()
            self.load_projects()
            self.load_branches()
        background(lambda: self.engine.set_token(token), done)

    def load_projects(self):
        if not self.engine.has_token:
            return
        self.project_dd.set_model(Gtk.StringList.new(["Loading projects…"]))
        self.project_dd.set_sensitive(False)

        def done(projects, err):
            if err:
                self.project_dd.set_model(Gtk.StringList.new(["Failed to load projects"]))
                self.set_status(self.project_status, str(err), "err")
                return
            self.projects = projects
            if not projects:
                self.project_dd.set_model(Gtk.StringList.new(["No projects on this account"]))
                return
            labels = [f"{p['name']}  ·  {p['ref']}  ·  {p['region']}"
                      + ("" if p["status"] == "ACTIVE_HEALTHY" else f"  ·  {p['status']}") for p in projects]
            self.project_dd.set_model(Gtk.StringList.new(labels))
            self.project_dd.set_selected(next((i for i, p in enumerate(projects)
                                               if p["ref"] == self.engine.prod_ref), 0))
            self.project_dd.set_sensitive(True)
            self.refresh_overview()
        background(self.engine.list_projects, done)

    def selected_project(self):
        i = self.project_dd.get_selected()
        return self.projects[i] if self.projects and 0 <= i < len(self.projects) else None

    def open_project_db_settings(self):
        p = self.selected_project()
        ref = p["ref"] if p else self.engine.prod_ref
        self.open_uri(f"https://supabase.com/dashboard/project/{ref}/database/settings" if ref
                      else "https://supabase.com/dashboard/projects")

    def on_project_apply(self):
        p = self.selected_project()
        password = self.project_pw.get_text()
        if not p:
            self.set_status(self.project_status, "Choose a project first.", "err")
            return
        if not password:
            self.set_status(self.project_status, "Enter the database password.", "err")
            return
        self.confirm("Repoint production?",
                     f"This changes which <b>production database</b> the tool reads from, to "
                     f"<b>{GLib.markup_escape_text(p['name'])}</b> ({p['ref']}). The connection is tested "
                     "read-only, then saved. Continue?", "Test & update",
                     lambda: self.apply_prod(lambda: self.engine.apply_prod_url(
                         self.engine.project_session_url(p["ref"], password)),
                         self.project_pw, self.use_project_btn, self.project_status))

    def on_url_apply(self):
        url = self.url_entry.get_text().strip()
        if not url:
            self.set_status(self.url_status, "Enter a connection URL first.", "err")
            return
        self.confirm("Repoint production?",
                     "This changes which <b>production database</b> the tool reads from. The new connection is "
                     "tested read-only, then saved. Continue?", "Test & update",
                     lambda: self.apply_prod(lambda: self.engine.apply_prod_url(url),
                                             self.url_entry, self.url_btn, self.url_status))

    def apply_prod(self, work, field, btn, status):
        btn.set_sensitive(False)
        self.set_status(status, "Testing connection…")

        def done(ref, err):
            btn.set_sensitive(True)
            if err:
                self.set_status(status, str(err), "err")
                return
            field.set_text("")
            self.set_status(status, f"Updated ✓ — connection verified ({ref}).", "ok")
            self.refresh_overview()
            self.load_branches()
        background(work, done)

    def on_pg_save(self):
        folder = self.pg_entry.get_text()
        self.set_status(self.pg_status, "Testing psql…")

        def done(version, err):
            if err:
                self.set_status(self.pg_status, str(err), "err")
                return
            self.set_status(self.pg_status, f"Saved ✓ — {version}", "ok")
            self.refresh_overview()
        background(lambda: self.engine.set_pg_bin(folder), done)

    # -- window --------------------------------------------------------------
    def show_about(self):
        Adw.AboutDialog(application_name="Prod Branch Sync", application_icon="prod-branch-sync",
                        version=VERSION, developer_name="eliau2005",
                        comments="Copy production Supabase data into a preview branch.",
                        website="https://github.com/eliau2005/prod-to-branch-sync",
                        license_type=Gtk.License.CUSTOM).present(self)

    def quit_now(self):
        abort_all()
        self.get_application().quit()

    def on_close_request(self, *_):
        if not self.syncing:
            return False
        self.confirm("Stop the running sync?",
                     "Quitting now aborts the restore. It runs in one transaction, so the branch is rolled back "
                     "to its previous data.", "Quit", self.quit_now)
        return True


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        GLib.set_application_name("Prod Branch Sync")
        Gtk.Window.set_default_icon_name("prod-branch-sync")

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()


def main():
    return App().run(sys.argv)
