stop vendor.qti.vibrator; rmmod haptic; insmod /data/local/tmp/hap/haptic.ko || echo INSMOD-FAIL
until dmesg | tail -50 | grep -q "ram_parse_wave_len: wave 4"; do sleep 0.5; done; start vendor.qti.vibrator; sleep 1
