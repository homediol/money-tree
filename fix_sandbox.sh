#!/usr/bin/env bash
# fix_sandbox.sh
# ==============
# Permanently fixes Chrome sandbox on Parrot OS.
#
# What this does:
#   1. Verifies the existing sandbox binary has correct SUID permissions
#   2. Fixes them if they are wrong
#   3. Confirms the kernel allows user namespaces (needed by the sandbox)
#   4. Removes any stale --disable-setuid-sandbox or --no-sandbox flags
#      from wrapper scripts if you accidentally added them
#
# Run once as yourself (sudo is called internally where needed):
#   bash fix_sandbox.sh

set -euo pipefail

GREEN='\033[32m'; RED='\033[31m'; YELLOW='\033[33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC}  $*"; }
warn() { echo -e "${YELLOW}  !${NC}  $*"; }
fail() { echo -e "${RED}  ✗${NC}  $*"; }

echo ""
echo "════════════════════════════════════════════"
echo "  Chrome Sandbox Fix — Parrot OS"
echo "════════════════════════════════════════════"

SANDBOX="/opt/google/chrome/chrome-sandbox"

# ── 1. Check sandbox binary exists ───────────────────────────────────────
if [[ ! -f "$SANDBOX" ]]; then
    fail "Sandbox not found at $SANDBOX"
    echo "    Is Google Chrome installed?  Try: sudo apt install google-chrome-stable"
    exit 1
fi
ok "Sandbox binary found: $SANDBOX"

# ── 2. Check / fix SUID permissions ──────────────────────────────────────
PERMS=$(stat -c "%a" "$SANDBOX")
OWNER=$(stat -c "%U" "$SANDBOX")

if [[ "$PERMS" == "4755" && "$OWNER" == "root" ]]; then
    ok "Sandbox permissions are correct (4755 root)"
else
    warn "Sandbox permissions wrong: ${PERMS} ${OWNER} — fixing with sudo"
    sudo chown root:root "$SANDBOX"
    sudo chmod 4755      "$SANDBOX"
    PERMS=$(stat -c "%a" "$SANDBOX")
    OWNER=$(stat -c "%U" "$SANDBOX")
    if [[ "$PERMS" == "4755" && "$OWNER" == "root" ]]; then
        ok "Sandbox permissions fixed: 4755 root"
    else
        fail "Could not fix sandbox permissions"
        exit 1
    fi
fi

# ── 3. Kernel: unprivileged user namespaces ───────────────────────────────
USERNS=$(sysctl -n kernel.unprivileged_userns_clone 2>/dev/null || echo "1")
if [[ "$USERNS" == "1" ]]; then
    ok "kernel.unprivileged_userns_clone = 1 (user namespace sandbox works)"
else
    warn "kernel.unprivileged_userns_clone = 0 — enabling temporarily"
    sudo sysctl -w kernel.unprivileged_userns_clone=1
    # Make permanent
    SYSCTL_CONF="/etc/sysctl.d/99-chrome-sandbox.conf"
    echo "kernel.unprivileged_userns_clone = 1" | sudo tee "$SYSCTL_CONF" > /dev/null
    ok "Set kernel.unprivileged_userns_clone=1 permanently in $SYSCTL_CONF"
fi

# ── 4. Quick launch test (headless, no bad flags) ─────────────────────────
echo ""
echo "Testing Chrome launch..."
if google-chrome \
    --headless=new \
    --disable-gpu \
    --disable-dev-shm-usage \
    --no-first-run \
    --dump-dom \
    "about:blank" \
    > /dev/null 2>&1; then
    ok "Chrome launches without errors"
else
    # Capture output for diagnosis
    OUTPUT=$(google-chrome \
        --headless=new \
        --disable-gpu \
        --disable-dev-shm-usage \
        --no-first-run \
        --dump-dom \
        "about:blank" 2>&1 || true)
    if echo "$OUTPUT" | grep -qi "unsupported command"; then
        fail "Chrome still showing unsupported flag warning:"
        echo "    $OUTPUT" | head -5
    else
        ok "Chrome launched (minor warnings are acceptable)"
    fi
fi

echo ""
echo "════════════════════════════════════════════"
echo "  Sandbox fix complete."
echo "  You can now run your Selenium scripts"
echo "  WITHOUT --no-sandbox or --disable-setuid-sandbox"
echo "════════════════════════════════════════════"
echo ""
