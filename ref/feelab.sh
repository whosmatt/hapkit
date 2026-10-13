#!/system/bin/sh
# RAM vs RTP brake A/B (needs test module haptic_rtpbrk.ko with /sys/module/haptic/parameters/rtp_brake). Root required.
#   sh /data/local/tmp/feelab.sh setup | teardown
#   sh /data/local/tmp/feelab.sh ram | rtpN | rtpY              # one wave-3 tick
#   sh /data/local/tmp/feelab.sh seriesram|seriesrtpN|seriesrtpY <interval_ms>   # 10 ticks
D=/data/local/tmp/feelab
N=/sys/class/leds/aw_vibrator
P=/sys/module/firmware_class/parameters/path
B=/sys/module/haptic/parameters/rtp_brake
[ "$(getprop ro.serialno)" = "214d028e" ] || { echo "wrong device"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "need root (adb root)"; exit 1; }
[ -e $B ] || { echo "rtp_brake parameter missing (test module not loaded)"; exit 1; }
evdev() { for e in /sys/class/input/event*; do [ "$(cat $e/device/name 2>/dev/null)" = "aw-haptic-hv" ] && { echo /dev/input/${e##*/}; return; }; done; }
need_setup() { [ -f $D/fwpath.orig ] || { echo "run setup first"; exit 1; }; }
mode() { case $1 in ram) echo ram;; rtpN) echo N > $B; echo rtp;; rtpY) echo Y > $B; echo rtp;; *) echo bad;; esac; }
case "$1" in
setup)
  [ -f $D/fwpath.orig ] || { cat $P > $D/fwpath.orig; getenforce > $D/selinux.orig; }
  setenforce 0
  echo -n "$D,$(cat $D/fwpath.orig)" > $P
  cp $D/wave3.bin $D/haptic_rtp_auto_sin.bin
  stop vendor.qti.vibrator
  echo "setup done: selinux=$(getenforce) hal=$(getprop init.svc.vendor.qti.vibrator) evdev=$(evdev) rtp_brake=$(cat $B)";;
teardown)
  [ -f $D/fwpath.orig ] && { cat $D/fwpath.orig > $P; rm $D/fwpath.orig; }
  rm -f $D/haptic_rtp_auto_sin.bin
  echo Y > $B
  echo "03 59" > $N/reg; echo "08 03" > $N/reg
  if [ -f $D/selinux.orig ]; then [ "$(cat $D/selinux.orig)" = Enforcing ] && setenforce 1; rm $D/selinux.orig; else setenforce 1; fi
  start vendor.qti.vibrator
  echo "teardown done: path=$(cat $P) selinux=$(getenforce) hal=$(getprop init.svc.vendor.qti.vibrator) rtp_brake=$(cat $B)";;
ram|rtpN|rtpY)
  need_setup; m=$(mode $1)
  EVDEV=$(evdev) $D/hapt $m ab_$1 100 1 0 >/dev/null
  echo "played 1x $1 (rtp_brake=$(cat $B))";;
seriesram|seriesrtpN|seriesrtpY)
  need_setup; x=${1#series}; iv=${2:?interval_ms}; m=$(mode $x)
  EVDEV=$(evdev) $D/hapt $m ab_${x}_$iv $iv 10 0 >/dev/null
  echo "played 10x $x every $iv ms (rtp_brake=$(cat $B))";;
*) echo "usage: $0 setup|teardown|ram|rtpN|rtpY|seriesram <ms>|seriesrtpN <ms>|seriesrtpY <ms>"; exit 1;;
esac
