"""Supabase look for the GTK app: the dashboard's palette, flat bordered cards, green brand.

Palette values are the same tokens public/index.html uses for the web UI. libadwaita's
own named colors are overridden too, so stock widgets (entries, popovers, dialogs) match.
"""
import json

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, GLib  # noqa: E402

from .engine import CONFIG_DIR  # noqa: E402

BRAND, BRAND_HOVER = "#3ECF8E", "#34b27b"
RED, RED_HOVER = "#dc2626", "#ef4444"

PALETTES = {
    # Supabase studio (dark)
    "dark": dict(page="#1c1c1c", card="#1f1f1f", bar="#171717", input="#2a2a2a", line="#2e2e2e",
                 line_h="#3e3e3e", fg="#ededed", muted="#a0a0a0", dim="#707070",
                 brand_text=BRAND, red_text="#f87171", amber="#fbbf24"),
    # Supabase dashboard (light)
    "light": dict(page="#fbfbfb", card="#ffffff", bar="#ffffff", input="#ffffff", line="#e6e6e6",
                  line_h="#d4d4d4", fg="#171717", muted="#6b7280", dim="#8f8f8f",
                  brand_text="#1f9d63", red_text=RED, amber="#b45309"),
}

LOGO_SVG = b"""<svg width="109" height="113" viewBox="0 0 109 113" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M63.7 110.3c-2.8 3.5-8.5 1.6-8.6-2.9L53.8 41h44.3c8 0 12.5 9.3 7.5 15.6l-41.9 53.7z" fill="#3ECF8E"/>
<path d="M63.7 110.3c-2.8 3.5-8.5 1.6-8.6-2.9L53.8 41h44.3c8 0 12.5 9.3 7.5 15.6l-41.9 53.7z" fill="url(#g)" fill-opacity="0.2"/>
<path d="M45.3 2.4c2.8-3.5 8.5-1.6 8.6 2.9l.6 66.4H10.8c-8 0-12.5-9.3-7.5-15.6L45.3 2.4z" fill="#3ECF8E"/>
<defs><linearGradient id="g" x1="53.97" y1="54.97" x2="94.16" y2="71.8" gradientUnits="userSpaceOnUse">
<stop/><stop offset="1" stop-opacity="0"/></linearGradient></defs></svg>"""

UI_FILE = CONFIG_DIR / "ui.json"


def logo_texture():
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(LOGO_SVG))
    except GLib.Error:
        return None


def saved_theme():
    try:
        theme = json.loads(UI_FILE.read_text()).get("theme")
    except (OSError, ValueError):
        theme = None
    return theme if theme in PALETTES else "dark"  # the dashboard's default


def save_theme(theme):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        UI_FILE.write_text(json.dumps({"theme": theme}))
    except OSError:
        pass


def _adw_colors(p, dark):
    """libadwaita named colors -> Supabase tokens."""
    return {
        "window-bg-color": p["page"], "window-fg-color": p["fg"],
        "view-bg-color": p["card"], "view-fg-color": p["fg"],
        "headerbar-bg-color": p["bar"], "headerbar-fg-color": p["fg"],
        "headerbar-border-color": p["line"], "headerbar-backdrop-color": p["bar"],
        "headerbar-shade-color": "transparent",
        "card-bg-color": p["card"], "card-fg-color": p["fg"], "card-shade-color": "transparent",
        "dialog-bg-color": p["card"], "dialog-fg-color": p["fg"],
        "popover-bg-color": p["card"], "popover-fg-color": p["fg"], "popover-shade-color": "transparent",
        "thumbnail-bg-color": p["card"],
        "accent-bg-color": BRAND, "accent-fg-color": "#171717", "accent-color": p["brand_text"],
        "destructive-bg-color": RED, "destructive-fg-color": "#ffffff", "destructive-color": p["red_text"],
        "success-color": p["brand_text"], "warning-color": p["amber"], "error-color": p["red_text"],
        "border-color": p["line"],
        "shade-color": "rgba(0,0,0,0.25)" if dark else "rgba(0,0,0,0.06)",
    }


def css(theme):
    p, dark = PALETTES[theme], theme == "dark"
    colors = _adw_colors(p, dark)
    if Adw.get_minor_version() >= 6:  # named colors became CSS variables in libadwaita 1.6
        root = ":root {\n" + "".join(f"  --{k}: {v};\n" for k, v in colors.items()) + "}\n"
    else:
        root = "".join(f"@define-color {k.replace('-', '_')} {v};\n" for k, v in colors.items())
    return root + f"""
window, dialog, popover {{ font-family: "Inter", "Ubuntu Sans", "Ubuntu", "Cantarell", sans-serif; }}
window.background {{ background: {p['page']}; color: {p['fg']}; }}
.mono, .mono label {{ font-family: "JetBrains Mono", "Ubuntu Sans Mono", "Ubuntu Mono", monospace; }}

/* Top bar: logo / breadcrumb, then underlined tabs, like the dashboard */
headerbar {{ background: {p['bar']}; box-shadow: none; border-bottom: none; min-height: 52px; }}
.sb-crumb-app {{ font-weight: 600; font-size: 15px; color: {p['fg']}; }}
.sb-crumb-sep {{ color: {p['dim']}; }}
.sb-crumb-page {{ color: {p['muted']}; font-size: 14px; }}
.sb-tabs {{ background: {p['bar']}; border-bottom: 1px solid {p['line']}; padding: 0 14px; }}
.sb-tabs viewswitcher button {{ background: none; border-radius: 0; margin: 0 10px 0 0; padding: 6px 2px;
  color: {p['muted']}; box-shadow: inset 0 -2px transparent; min-height: 30px; }}
.sb-tabs viewswitcher button:hover {{ color: {p['fg']}; }}
.sb-tabs viewswitcher button:checked {{ color: {p['fg']}; box-shadow: inset 0 -2px {BRAND}; }}
.sb-tabs viewswitcher button image {{ -gtk-icon-size: 0; min-width: 0; margin: 0; }}
.sb-icon-btn {{ border: 1px solid {p['line']}; border-radius: 6px; background: {p['page']};
  min-width: 34px; min-height: 34px; padding: 0; color: {p['muted']}; }}
.sb-icon-btn:hover {{ color: {p['fg']}; border-color: {p['line_h']}; }}

/* Page headings */
.sb-h1 {{ font-size: 20px; font-weight: 600; color: {p['fg']}; letter-spacing: -0.01em; }}
.sb-lead {{ color: {p['muted']}; font-size: 14px; }}

/* Cards: flat, 1px border, 6px radius */
.sb-card {{ background: {p['card']}; border: 1px solid {p['line']}; border-radius: 6px; }}
.sb-card-head {{ padding: 12px 20px; border-bottom: 1px solid {p['line']}; }}
.sb-card-title {{ font-weight: 600; font-size: 14px; color: {p['fg']}; }}
.sb-card-desc {{ color: {p['muted']}; font-size: 12px; }}
.sb-card-body {{ padding: 16px 20px; }}
.sb-card list, .sb-card list > row {{ background: transparent; }}
.sb-card list > row {{ border-color: {p['line']}; }}
.sb-card list > row:not(:last-child) {{ border-bottom: 1px solid {p['line']}; }}
.sb-card row.property .title, .sb-card row .sb-dt {{ color: {p['muted']}; font-size: 13px; }}
.sb-dd {{ color: {p['fg']}; font-size: 13px; }}
.sb-card.danger {{ border-color: alpha({RED}, 0.45); }}
.sb-card.danger .sb-card-head {{ background: alpha({RED}, 0.06); border-bottom-color: alpha({RED}, 0.3); }}
.sb-card.danger .sb-card-head .sb-card-title {{ color: {p['red_text']}; }}

/* Inputs and selects: input bg, 1px border, brand focus ring */
.sb-card entry, .sb-card passwordentry, .sb-select > button {{ background: {p['input']}; color: {p['fg']};
  border: 1px solid {p['line']}; border-radius: 6px; box-shadow: none; min-height: 34px; outline: none; }}
.sb-select > button:hover {{ border-color: {p['line_h']}; }}
.sb-card entry:focus-within, .sb-card passwordentry:focus-within {{ border-color: {BRAND};
  box-shadow: 0 0 0 1px alpha({BRAND}, 0.4); }}
.sb-card.danger entry:focus-within, .sb-card.danger passwordentry:focus-within {{ border-color: {RED};
  box-shadow: 0 0 0 1px alpha({RED}, 0.4); }}
.sb-card entry > text > placeholder, .sb-card passwordentry > text > placeholder {{ color: {p['dim']}; }}
.sb-card expander-widget > box > title, .sb-card expander title {{ color: {p['muted']}; }}
.sb-card separator {{ background: {p['line']}; }}
popover.menu > contents, popover > contents {{ border: 1px solid {p['line']}; border-radius: 6px; }}

/* Buttons */
button.sb-primary {{ background: {BRAND}; color: #171717; font-weight: 600; border-radius: 6px;
  padding: 6px 16px; box-shadow: none; }}
button.sb-primary:hover {{ background: {BRAND_HOVER}; }}
button.sb-primary:disabled {{ opacity: 0.4; }}
button.sb-danger {{ background: {RED}; color: #ffffff; font-weight: 500; border-radius: 6px; padding: 6px 16px; }}
button.sb-danger:hover {{ background: {RED_HOVER}; }}
button.sb-secondary {{ background: {p['input']}; color: {p['fg']}; border: 1px solid {p['line']};
  border-radius: 6px; padding: 6px 12px; }}
button.sb-secondary:hover {{ border-color: {p['line_h']}; }}
button.sb-link {{ background: none; color: {p['brand_text']}; font-size: 12px; padding: 2px 4px; min-height: 0; }}
button.sb-link:hover {{ color: {BRAND_HOVER}; }}

/* Pills, callouts, steps, log */
.sb-pill {{ border: 1px solid {p['line']}; border-radius: 999px; padding: 1px 9px; font-size: 12px; color: {p['muted']}; }}
.sb-pill.ok {{ border-color: alpha({BRAND}, 0.4); color: {p['brand_text']}; }}
.sb-pill.bad {{ border-color: alpha({RED}, 0.4); color: {p['red_text']}; }}
.sb-callout {{ border-radius: 6px; padding: 8px 12px; font-size: 12px; }}
.sb-callout.warn {{ background: alpha({p['amber']}, 0.08); border: 1px solid alpha({p['amber']}, 0.3); color: {p['amber']}; }}
.sb-callout.err {{ background: alpha({RED}, 0.08); border: 1px solid alpha({RED}, 0.3); color: {p['red_text']}; font-size: 13px; }}
.sb-callout.ok {{ background: alpha({BRAND}, 0.1); border: 1px solid alpha({BRAND}, 0.3); color: {p['brand_text']};
  font-size: 13px; font-weight: 500; }}
.sb-step {{ border: 1px solid {p['line']}; border-radius: 999px; padding: 3px 12px; font-size: 12px; color: {p['muted']}; }}
.sb-step.active {{ border-color: {BRAND}; color: {p['brand_text']}; background: alpha({BRAND}, 0.1); }}
.sb-step.done {{ border-color: alpha({BRAND}, 0.4); color: {p['brand_text']}; }}
.sb-arrow {{ color: {p['dim']}; }}
.sb-log, .sb-log text {{ background: {p['bar']}; color: {p['muted']}; }}
.sb-log {{ font-size: 12px; padding: 14px; }}
.sb-log-frame {{ border: 1px solid {p['line']}; border-radius: 6px; background: {p['bar']}; }}
.sb-ok {{ color: {p['brand_text']}; }}
.sb-bad {{ color: {p['red_text']}; }}
.sb-footer {{ color: {p['dim']}; font-size: 11px; }}
"""
