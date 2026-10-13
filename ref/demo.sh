#!/system/bin/sh
# Haptics demo: patterns the OS doesn't reach, then strength ladder. Run as root.
V=/sys/class/leds/aw_vibrator
H=android.hardware.vibrator.IVibrator/default
gap() { sleep ${1:-3}; }
step() { echo "[$(date +%T)] $*"; }
ram() { service call $H 4 i32 $1 i32 2 null >/dev/null; }   # HAL perform(effect=N), no framework off()
rtp() { echo $1 > $V/rtp; }
echo 8500 > $V/vmax; echo 0x80 > $V/gain

step "1  today's UI click (framework fallback: 20ms loop, boost bypassed)"; cmd vibrator_manager feedback 1; gap
step "2  RAM wave 1 (30ms, boosted)";  ram 1; gap
step "3  RAM wave 2 (22ms, boosted)";  ram 2; gap
step "4  RAM wave 3 (15ms, boosted)";  ram 3; gap
step "5  RAM wave 1 x3, 150ms apart";  ram 1; sleep 0.15; ram 1; sleep 0.15; ram 1; gap
step "6  RTP stock pattern, first 6s";  rtp 1; sleep 6; echo 0 > $V/rtp; gap 4

step "7  today's max constant: oneshot 1s amp 255 (boost bypassed)"; cmd vibrator_manager synced oneshot 1000 255; gap 4
step "8  232Hz 1s, gain 0x20 (25%), 8.5V boost"; echo 0x20 > $V/gain; rtp 2; gap
step "9  232Hz 1s, gain 0x40 (50%)";              echo 0x40 > $V/gain; rtp 2; gap
step "10 232Hz 1s, gain 0x80 (100%)";             echo 0x80 > $V/gain; rtp 2; gap
step "11 232Hz 1s, gain 0x80, 11V boost (max)";   echo 11000 > $V/vmax; rtp 2; gap
step "12 RAM wave 1 at 11V (compare step 2)";     ram 1; gap
step "13 sweep 50->500Hz 6s, 8.5V";               echo 8500 > $V/vmax; rtp 3; sleep 7

echo 8500 > $V/vmax; echo 0x80 > $V/gain; step done
