#define _GNU_SOURCE
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <pthread.h>
#include <linux/input.h>
#include <sys/ioctl.h>
#define N "/sys/class/leds/aw_vibrator/"
static int kfd, pollmode; static volatile int stop_poll;
static const char *tag;
static void klog(const char *fmt, ...) { char b[200]; va_list ap; va_start(ap, fmt); int n=vsnprintf(b,sizeof b,fmt,ap); va_end(ap); write(kfd,b,n); }
static int rd_glb(int afd) { char b[32]; lseek(afd,0,SEEK_SET); write(afd,"1 1 0x3f",8); int n=pread(afd,b,sizeof b-1,0); if(n<=0) return -1; b[n]=0; return (int)strtol(b,NULL,16); }
static void *poller(void *a) {
  int afd=open(N "awrw",O_RDWR), rfd=open(N "reg",O_WRONLY); int last=-2, n=0, clr=0;
  while(!stop_poll) {
    int v=rd_glb(afd); n++;
    if(v!=last) {
      if(pollmode==2 && v==8) { write(rfd,"08 01",5); clr++; klog("TK %s glb=0x%02x clr n=%d\n",tag,v,n); }
      else klog("TK %s glb=0x%02x n=%d\n",tag,v,n);
      last=v;
    }
  }
  klog("TK %s pollend n=%d clr=%d\n",tag,n,clr);
  return 0;
}
int main(int c, char **v) {
  if(c<6){fprintf(stderr,"hapt ram|rtp tag interval_ms count pollmode(0 none,1 log,2 log+clear brk)\n");return 1;}
  const char *mode=v[1]; tag=v[2]; int iv=atoi(v[3]), cnt=atoi(v[4]); pollmode=atoi(v[5]);
  kfd=open("/dev/kmsg",O_WRONLY);
  int efd=-1, rtpfd=-1; struct ff_effect e;
  if(!strcmp(mode,"ram")) {
    efd=open(getenv("EVDEV")?getenv("EVDEV"):"/dev/input/event2",O_RDWR); if(efd<0){perror("event2");return 1;}
    memset(&e,0,sizeof e); static short cd[3]={2,0,0}; if(getenv("EFF")) cd[0]=atoi(getenv("EFF"));
    e.type=FF_PERIODIC; e.id=-1; e.u.periodic.waveform=FF_CUSTOM; e.u.periodic.magnitude=0x7fff;
    e.u.periodic.custom_len=3; e.u.periodic.custom_data=cd; e.replay.length=0;
    if(ioctl(efd,EVIOCSFF,&e)<0){perror("EVIOCSFF");return 1;}
    printf("uploaded id=%d play_ms=%d.%03d\n",e.id,cd[1],cd[2]);
  } else { rtpfd=open(N "rtp",O_WRONLY); if(rtpfd<0){perror("rtp");return 1;} }
  pthread_t th; if(pollmode) { pthread_create(&th,0,poller,0); usleep(20000); }
  struct timespec t0; clock_gettime(CLOCK_MONOTONIC,&t0);
  for(int i=0;i<cnt;i++) {
    struct timespec t=t0; long long ns=(long long)t0.tv_nsec+(long long)i*iv*1000000LL;
    t.tv_sec+=ns/1000000000LL; t.tv_nsec=ns%1000000000LL;
    clock_nanosleep(CLOCK_MONOTONIC,TIMER_ABSTIME,&t,0);
    klog("TK %s i=%d trig\n",tag,i);
    if(efd>=0){ struct input_event ev; memset(&ev,0,sizeof ev); ev.type=EV_FF; ev.code=e.id; ev.value=1; write(efd,&ev,sizeof ev); }
    else write(rtpfd,"4",1);
  }
  usleep(250000);
  if(pollmode){ stop_poll=1; pthread_join(th,0); }
  klog("TK %s done\n",tag);
  if(efd>=0){ ioctl(efd,EVIOCRMFF,e.id); close(efd);} 
  return 0;
}
