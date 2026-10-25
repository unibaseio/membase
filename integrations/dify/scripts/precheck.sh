#!/usr/bin/env bash
# Run the checks of langgenius/dify-plugins' "Pre Check Plugin" workflow
# (.github/workflows/pre-check-plugin.yaml) on the built package, locally.
#
#   scripts/precheck.sh [path/to/membase-X.Y.Z.difypkg] [path/to/pr-body.md]
#
# Tools are fetched into .cache/ (gitignored): dify-marketplace-toolkit, the
# dify-plugin daemon/CLI binary, yq. Needs uv, git, gh (or curl), unzip; the
# install test runs in a private network namespace (unshare) so that port 8080
# is the plugin's own even if something on this machine already listens there.
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
CACHE="$HERE/.cache"
PKG=$(realpath "${1:-$HERE/membase-0.0.1.difypkg}")
PR_BODY=$(realpath "${2:-$HERE/pr-body.md}")
TOOLKIT="$CACHE/dify-marketplace-toolkit"
DAEMON="$CACHE/dify-plugin-linux-amd64"
MARKETPLACE=https://marketplace.dify.ai
mkdir -p "$CACHE"

[ -d "$TOOLKIT" ] || git clone -q --depth 1 https://github.com/langgenius/dify-marketplace-toolkit "$TOOLKIT"
if [ ! -x "$DAEMON" ]; then
  gh release download -R langgenius/dify-plugin-daemon --pattern dify-plugin-linux-amd64 --dir "$CACHE"
  chmod +x "$DAEMON"
fi
if ! command -v yq >/dev/null; then
  if [ ! -x "$CACHE/yq" ]; then
    gh release download -R mikefarah/yq --pattern yq_linux_amd64 --dir "$CACHE"
    mv "$CACHE/yq_linux_amd64" "$CACHE/yq" && chmod +x "$CACHE/yq"
  fi
  export PATH="$CACHE:$PATH"
fi

# CI runs the validators on Python 3.12 with `requests` installed.
TOOLS_VENV="$CACHE/precheck-tools"
[ -x "$TOOLS_VENV/bin/python" ] || { uv venv -q --python 3.12 "$TOOLS_VENV" && uv pip install -q --python "$TOOLS_VENV/bin/python" requests; }
PY="$TOOLS_VENV/bin/python"

WORK=$(mktemp -d "$CACHE/precheck.XXXXXX")   # on disk, not a small /tmp
[ -n "${KEEP_WORK:-}" ] || trap 'rm -rf "$WORK"' EXIT
OUT="$WORK/out"
mkdir -p "$OUT"
cp "$PKG" "$WORK/plugin.difypkg.zip"
unzip -q "$WORK/plugin.difypkg.zip" -d "$WORK/unpacked_plugin"
P="$WORK/unpacked_plugin"
V="$TOOLKIT/validator"
echo "package: $PKG"
echo "work dir: $WORK"

run() { echo "--- $1"; shift; "$@" || true; }
run contents   "$PY" "$V/check-package-contents.py" -d "$P" --package-file "$WORK/plugin.difypkg.zip" --error-file "$OUT/package_contents_errors.txt" --warning-file "$OUT/package_contents_warnings.txt"
run secrets    "$PY" "$V/check-package-secrets.py" -d "$P" --error-file "$OUT/package_secrets_errors.txt" --warning-file "$OUT/package_secrets_warnings.txt"
run binaries   "$PY" "$V/check-package-binaries.py" -d "$P" --error-file "$OUT/package_binaries_errors.txt" --warning-file "$OUT/package_binaries_warnings.txt"
run manifest   "$PY" "$V/check-manifest-metadata.py" -d "$P" --error-file "$OUT/manifest_errors.txt" --warning-file "$OUT/manifest_warnings.txt"
run readme     "$PY" "$V/check-readme-metadata.py" -d "$P" --error-file "$OUT/readme_errors.txt" --warning-file "$OUT/readme_warnings.txt"
run pr-body    "$PY" "$V/check-pr-body.py" --pr-body-file "$PR_BODY" --error-file "$OUT/pr_body_errors.txt"
run pr-notes   "$PY" "$V/check-pr-review-notes.py" --pr-body-file "$PR_BODY" --warning-file "$OUT/pr_review_notes_warnings.txt"
run version    "$PY" "$V/check-version-update.py" -d "$P" --package-path "$WORK/plugin.difypkg.zip" --pr-body-file "$PR_BODY" --error-file "$OUT/version_update_errors.txt" --warning-file "$OUT/version_update_warnings.txt"
run deps       "$PY" "$V/check-package-dependencies.py" -d "$P" --error-file "$OUT/package_dependencies_errors.txt" --warning-file "$OUT/package_dependencies_warnings.txt"
run sensitive  "$PY" "$V/check-sensitive-capabilities.py" -d "$P" --pr-body-file "$PR_BODY" --error-file "$OUT/sensitive_capabilities_errors.txt" --warning-file "$OUT/sensitive_capabilities_warnings.txt"
run safety     "$PY" "$V/check-python-safety-warnings.py" -d "$P" --warning-file "$OUT/python_safety_warnings.txt"
run financial  "$PY" "$V/check-prohibited-financial-activity.py" -d "$P" --warning-file "$OUT/prohibited_financial_activity_warnings.txt"

echo "--- icon"
ICON=$(yq '.icon' "$P/manifest.yaml")
if [ ! -f "$P/_assets/$ICON" ]; then echo "icon file not found: _assets/$ICON" >> "$OUT/icon_errors.txt"
elif grep -q DIFY_MARKETPLACE_TEMPLATE_ICON_DO_NOT_USE "$P/_assets/$ICON"; then echo "template icon" >> "$OUT/icon_errors.txt"
else echo "icon ok: _assets/$ICON"; fi

echo "--- version exists on $MARKETPLACE"
read -r NAME AUTHOR VERSION < <(yq -r '[.name, .author, .version] | join(" ")' "$P/manifest.yaml")
CODE=$(curl -s -o "$WORK/version.json" -w '%{http_code}' --max-time 30 "$MARKETPLACE/api/v1/plugins/$AUTHOR/$NAME/$VERSION" || true)
if [ "$CODE" = 200 ] && [ "$(yq -p json -r '.code' "$WORK/version.json")" = 0 ]; then
  echo "$AUTHOR/$NAME $VERSION already exists on the Marketplace" >> "$OUT/version_errors.txt"
else echo "version $VERSION is free (HTTP $CODE)"; fi

echo "--- dependencies + install test (python -m main, serverless on :8080)"
uv venv -q --python 3.12 "$WORK/venv"
if uv pip install -q --python "$WORK/venv/bin/python" -r "$P/requirements.txt" packaging requests >"$WORK/pip.log" 2>&1; then
  (
    export PATH="$WORK/venv/bin:$PATH" INSTALL_METHOD=serverless SERVERLESS_PORT=8080 SERVERLESS_HOST=0.0.0.0
    unshare -rn sh -c "ip link set lo up && timeout 300 python3 '$V/test-plugin-install.py' -d '$P'"
  ) || echo "plugin installation test failed" >> "$OUT/install_errors.txt"
else
  { echo "failed to install plugin dependencies"; tail -20 "$WORK/pip.log"; } >> "$OUT/install_errors.txt"
fi

echo "--- packaging (uploader --test with the plugin daemon)"
(cd "$WORK" && timeout 300 "$PY" "$TOOLKIT/uploader" -d "$P" -t placeholder --plugin-daemon-path "$DAEMON" -u "$MARKETPLACE" -f --test) \
  || echo "packaging check failed" >> "$OUT/packaging_errors.txt"

echo
echo "=== Pre-check summary"
FAIL=0
for f in "$OUT"/*_errors.txt; do
  [ -s "$f" ] || continue
  FAIL=1; echo "BLOCKING $(basename "$f"):"; sed 's/^/  /' "$f"
done
for f in "$OUT"/*_warnings.txt; do
  [ -s "$f" ] || continue
  echo "warning $(basename "$f"):"; sed 's/^/  /' "$f"
done
[ "$FAIL" = 0 ] && echo "No blocking errors." || { echo "Blocking errors found."; exit 1; }
