#!/usr/bin/env bash
#
# build-deb.sh — build the Ubuntu/Debian package (.deb) of the native GTK4 app.
#
# The Ubuntu app is Python + GTK4 + libadwaita (linux/prod_branch_sync/), so the package
# is architecture-independent and pulls everything it needs from the Ubuntu archive:
# PyGObject, GTK 4, libadwaita >= 1.5 and the PostgreSQL client tools (>= 17).
#
# Usage:
#   bash packaging/build-deb.sh                  # version from package.json
#   bash packaging/build-deb.sh --version 1.2.0

set -euo pipefail
umask 022

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$PKG_DIR")"
DIST="$ROOT/dist"
NAME=prod-branch-sync
APP_ID=io.github.eliau2005.ProdBranchSync

VERSION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

info() { printf '\033[36m[build-deb]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[build-deb] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v dpkg-deb >/dev/null || die "dpkg-deb not found (install the dpkg package)."
[[ -n "$VERSION" ]] || VERSION="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['version'])" "$ROOT/package.json")"
info "version: $VERSION"

info "Checking the SQL port against lib/sync-sql.mjs…"
python3 "$ROOT/test/parity.py"

STAGE="$DIST/deb/${NAME}_${VERSION}_all"
rm -rf "$STAGE"
LIB="$STAGE/usr/lib/$NAME"
mkdir -p "$LIB/prod_branch_sync" "$STAGE/usr/bin" "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/usr/share/doc/$NAME" "$STAGE/DEBIAN"

info "Staging app files…"
install -m 644 "$ROOT"/linux/prod_branch_sync/*.py "$LIB/prod_branch_sync/"
# Keep the package's VERSION in step with the one being built.
sed -i "s/^VERSION = .*/VERSION = \"$VERSION\"/" "$LIB/prod_branch_sync/__init__.py"
install -m 755 "$PKG_DIR/linux/$NAME" "$STAGE/usr/bin/$NAME"
install -m 644 "$PKG_DIR/linux/$APP_ID.desktop" "$STAGE/usr/share/applications/"
install -m 644 "$PKG_DIR/linux/$NAME.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/"
install -m 644 "$ROOT/README.md" "$STAGE/usr/share/doc/$NAME/"

SIZE_KB="$(du -sk "$STAGE" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<CTRL
Package: $NAME
Version: $VERSION
Architecture: all
Maintainer: eliau2005 <eliau2005@users.noreply.github.com>
Installed-Size: $SIZE_KB
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), postgresql-client-18 | postgresql-client-17 | postgresql-client-19
Recommends: fonts-inter
Section: devel
Priority: optional
Homepage: https://github.com/eliau2005/prod-to-branch-sync
Description: Copy production Supabase data into a preview branch
 Native GTK4/libadwaita app that dumps a production Supabase database (public
 schema plus auth logins) and restores it into a preview branch, handling
 schema drift between the two. Pick the production project from your account,
 pick a branch, and it verifies row counts afterwards.
CTRL

# Compile bytecode on install, remove it on uninstall (Debian python policy, simplified).
cat > "$STAGE/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -e
python3 -m compileall -q /usr/lib/prod-branch-sync >/dev/null 2>&1 || true
SH
cat > "$STAGE/DEBIAN/prerm" <<'SH'
#!/bin/sh
set -e
find /usr/lib/prod-branch-sync -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
SH
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"

mkdir -p "$DIST"
OUT="$DIST/${NAME}_${VERSION}_all.deb"
info "Building package…"
dpkg-deb --build --root-owner-group -Zxz "$STAGE" "$OUT" >/dev/null
info "DONE → $OUT"
