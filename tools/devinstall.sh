#!/bin/bash
# Install a hapkit project on the dev device so the framework uses it until removed (survives reboots):
#   haptic_ram.bin  -> /vendor/firmware (adb remount overlay)
#   haptic.ko       -> /vendor/etc/hapkit, built from KWT with the project's effect table
#   boot hook       -> /system/etc/init/hapkit.rc + /system/etc/hapkit/swap.sh: swaps the module in post-fs-data,
#                      before the vibrator HAL starts
# usage: tools/devinstall.sh [project]          install and reboot
#        tools/devinstall.sh --remove           delete everything above and reboot (back to stock)
# Needs: KWT = kernel worktree with the TEST ONLY primitives patch (default ~/lineage/haptics-prim-wt),
#        KBUILD = script whose K=/O=/MK lines describe the kernel build (default ~/lineage/build_kernel_haptic24.sh),
#        a userdebug build with root, adb remount working, and the device serial below.
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
ADB=${HAPKIT_ADB:-/mnt/c/Users/matt/adb-fastboot/platform-tools/adb.exe}
SERIAL=${HAPKIT_SERIAL:-214d028e}
KWT=${KWT:-$HOME/lineage/haptics-prim-wt}
KBUILD=${KBUILD:-$HOME/lineage/build_kernel_haptic24.sh}
A=("$ADB" -s "$SERIAL")
sh() { "${A[@]}" shell "$@" | tr -d '\r'; }
push() { "${A[@]}" push "$(wslpath -w "$1" 2>/dev/null || echo "$1")" "$2" >/dev/null; }

[ "$(sh getprop ro.serialno)" = "$SERIAL" ] || { echo "device $SERIAL not attached"; exit 1; }
"${A[@]}" root >/dev/null; sleep 2
"${A[@]}" remount >/dev/null

if [ "${1:-}" = "--remove" ]; then
    sh 'rm -rf /system/etc/init/hapkit.rc /system/etc/hapkit /vendor/etc/hapkit /vendor/firmware/haptic_ram.bin'
    echo "removed; rebooting"; "${A[@]}" reboot; exit 0
fi

PROJECT=${1:-$HERE/projects/fp6.hapkit.json}
OUT=$(mktemp -d); trap 'rm -rf "$OUT"' EXIT
PYTHONPATH="$HERE" python3 - "$PROJECT" "$OUT" <<'EOF'
import sys
from hapkit.core import exports, project as proj
p = proj.load(sys.argv[1])
blob, rep = exports.export_ram_bin(p, upstream=True)
open(f"{sys.argv[2]}/haptic_ram.bin", "wb").write(blob)
table = exports.export_kernel_table(p, upstream=True)
open(f"{sys.argv[2]}/haptic_hv_effects.h", "w", newline="\r\n").write(table)
print(f"{p['name']}: haptic_ram.bin {rep['sha256'][:12]} ({rep['file_bytes']} B)")
EOF

cp "$OUT/haptic_hv_effects.h" "$KWT/drivers/misc/haptic_hv/haptic_hv_effects.h"
eval "$(sed -n '/^K=/,/LIBCLANG_PATH/p' "$KBUILD")"
"${MK[@]}" M="$KWT/drivers/misc/haptic_hv" modules 2>&1 | grep -E "warning|error" && exit 1 || true
"$CL/llvm-strip" --strip-debug -o "$OUT/haptic.ko" "$KWT/drivers/misc/haptic_hv/haptic.ko"

cat > "$OUT/hapkit.rc" <<'EOF'
# hapkit test setup (adb remount overlay; userdebug only). Swaps haptic.ko for /vendor/etc/hapkit/haptic.ko
# before the vibrator HAL starts (class hal). Back to stock: hapkit tools/devinstall.sh --remove.
on post-fs-data
    exec u:r:su:s0 root root -- /system/bin/sh /system/etc/hapkit/swap.sh
EOF
cat > "$OUT/swap.sh" <<'EOF'
#!/system/bin/sh
# hapkit: replace the stock haptic.ko with /vendor/etc/hapkit/haptic.ko (see /system/etc/init/hapkit.rc)
ko=/vendor/etc/hapkit/haptic.ko
[ -f "$ko" ] || exit 0
if grep -q '^haptic ' /proc/modules; then
    rmmod haptic || { log -p e -t hapkit "rmmod haptic failed"; exit 1; }
fi
insmod "$ko" || { log -p e -t hapkit "insmod $ko failed"; exit 1; }
# the QTI vibrator HAL (user system) opens primitive_duration O_RDWR
n=/sys/class/qcom-haptics/primitive_duration
for i in 1 2 3 4 5 6 7 8 9 10; do [ -e $n ] && break; sleep 0.1; done
chown system:system $n && chmod 0664 $n
log -t hapkit "haptic.ko swapped ($(sha256sum "$ko" | cut -c1-12)), ram $(sha256sum /vendor/firmware/haptic_ram.bin | cut -c1-12)"
EOF

sh 'mkdir -p /vendor/etc/hapkit /system/etc/hapkit'
push "$OUT/haptic.ko" /vendor/etc/hapkit/haptic.ko
push "$OUT/haptic_ram.bin" /vendor/firmware/haptic_ram.bin
push "$OUT/hapkit.rc" /system/etc/init/hapkit.rc
push "$OUT/swap.sh" /system/etc/hapkit/swap.sh
sh 'chmod 644 /vendor/etc/hapkit/haptic.ko /vendor/firmware/haptic_ram.bin /system/etc/init/hapkit.rc;
    chmod 755 /system/etc/hapkit/swap.sh;
    restorecon -R /vendor/etc/hapkit /vendor/firmware/haptic_ram.bin /system/etc/hapkit /system/etc/init/hapkit.rc 2>/dev/null' >/dev/null
echo "installed; rebooting"
"${A[@]}" reboot
