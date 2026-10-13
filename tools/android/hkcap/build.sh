#!/bin/bash
# Build hkcap.dex with the JDK, android.jar and d8 from the LineageOS tree (read-only use of prebuilts).
set -euo pipefail
LOS=${LOS:-$HOME/lineage}
JDK=$LOS/prebuilts/jdk/jdk21/linux-x86
SDK=$LOS/prebuilts/sdk/36/public/android.jar
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
"$JDK/bin/javac" --release 11 -cp "$SDK" -d "$OUT" "$HERE/HkCap.java"
"$JDK/bin/java" -cp "$LOS/prebuilts/r8/r8.jar" com.android.tools.r8.D8 --min-api 30 --lib "$SDK" \
    --output "$OUT" "$OUT"/*.class
cp "$OUT/classes.dex" "$HERE/hkcap.dex"
echo "built $HERE/hkcap.dex"
