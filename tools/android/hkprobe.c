/*
 * hkprobe: trigger a haptic_hv playback through a sysfs node and log AW8693x GLBRD5 state transitions.
 * usage: hkprobe <node> <value> [timeout_ms] [repeat]
 *   e.g. hkprobe /sys/class/leds/aw_vibrator/brightness 1 300 3
 * Output: one line per repeat: "run <n> t_us:state ..." (state 0 standby, 7 ram_go, 8 rtp_go, 0xb brake).
 * build: aarch64-linux-gnu-gcc -static -O2 -o hkprobe hkprobe.c
 */
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define AWRW "/sys/class/leds/aw_vibrator/awrw"

static long long now_us(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000000LL + ts.tv_nsec / 1000;
}

static int read_state(int fd)
{
	char buf[32];
	int n;

	if (pwrite(fd, "1 1 0x3f", 8, 0) < 0)
		return -1;
	n = pread(fd, buf, sizeof(buf) - 1, 0);
	if (n <= 0)
		return -1;
	buf[n] = 0;
	return (int)strtol(buf, NULL, 16) & 0x0f;
}

int main(int argc, char **argv)
{
	int afd, tfd, run, repeat, timeout_ms;

	if (argc < 3) {
		fprintf(stderr, "usage: %s <node> <value> [timeout_ms] [repeat]\n", argv[0]);
		return 2;
	}
	timeout_ms = argc > 3 ? atoi(argv[3]) : 300;
	repeat = argc > 4 ? atoi(argv[4]) : 1;
	afd = open(AWRW, O_RDWR);
	tfd = open(argv[1], O_WRONLY);
	if (afd < 0 || tfd < 0) {
		perror("open");
		return 1;
	}
	for (run = 0; run < repeat; run++) {
		long long t0, t;
		int last = -2, left = 0, s, polls = 0;

		while (read_state(afd) != 0 && now_us() < 0)
			;
		usleep(20000);
		t0 = now_us();
		if (write(tfd, argv[2], strlen(argv[2])) < 0) {
			perror("trigger");
			return 1;
		}
		printf("run %d", run);
		while ((t = now_us()) - t0 < timeout_ms * 1000LL) {
			s = read_state(afd);
			polls++;
			if (s != last) {
				printf(" %lld:%x", t - t0, s);
				last = s;
			}
			if (s != 0)
				left = 1;
			else if (left)
				break;
		}
		printf(" polls=%d\n", polls);
		fflush(stdout);
		usleep(150000);
	}
	return 0;
}
