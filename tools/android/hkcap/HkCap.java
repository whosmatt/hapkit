/*
 * hkcap: record the microphone (48 kHz WAV) or a sensor at its fastest rate (CSV) while firing a trigger command.
 * Runs via app_process (no APK), as root so the HIGH_SAMPLING_RATE_SENSORS cap does not apply.
 *
 * usage: CLASSPATH=/data/local/tmp/hapkit/hkcap.dex app_process / HkCap [-s mic[:src]|accel|uncal|gyro|<type>]
 *          [-p period_us] [-d duration_ms] [-w trigger_at_ms] [-c "shell cmd"] [-o out] [-l]
 *   mic: AudioRecord source src (default 9 UNPROCESSED, which records silence on FP6; use 6 VOICE_RECOGNITION,
 *   no AGC/NS per CDD). Sensors: CSV t_ns (sensor timestamp, CLOCK_BOOTTIME), x, y, z. -l lists sensors.
 *   start_ns/trigger_ns (BOOTTIME just before the command ran) go in a CSV comment or the WAV ICMT chunk.
 *   On FP6 the IMU (icm4x6xx on the ADSP sensor island) tops out at 500 Hz with its low-pass far below f0.
 * build: tools/android/hkcap/build.sh
 */
import android.content.Context;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.SystemClock;

import java.io.FileOutputStream;
import java.io.PrintStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.List;

public class HkCap {
    static final int CAP = 1 << 20;
    static final long[] ts = new long[CAP];
    static final float[] xyz = new float[CAP * 3];
    static volatile int n = 0;

    static Context systemContext() throws Exception {
        Class<?> at = Class.forName("android.app.ActivityThread");
        Constructor<?> c = at.getDeclaredConstructor();
        c.setAccessible(true);
        Object thread = c.newInstance();
        Field cur = at.getDeclaredField("sCurrentActivityThread");
        cur.setAccessible(true);
        cur.set(null, thread);
        Method m = at.getDeclaredMethod("getSystemContext");
        return (Context) m.invoke(thread);
    }

    /* -s mic[:source] (default UNPROCESSED=9; 6 VOICE_RECOGNITION, 1 MIC): 48 kHz mono 16-bit WAV to -o. */
    static void recordMic(String which, int durMs, int trigMs, String cmd, String out) throws Exception {
        int src = which.contains(":") ? Integer.parseInt(which.substring(which.indexOf(':') + 1)) : 9;
        int fs = 48000;
        int min = AudioRecord.getMinBufferSize(fs, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord ar = new AudioRecord(src, fs, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                Math.max(min, fs / 5 * 2));
        if (ar.getState() != AudioRecord.STATE_INITIALIZED) { System.err.println("AudioRecord init failed"); System.exit(1); }
        int total = (int) ((long) fs * durMs / 1000);
        short[] pcm = new short[total];
        ar.startRecording();
        long t0 = SystemClock.elapsedRealtimeNanos();
        final long[] trig = {0};
        Thread tr = null;
        if (cmd != null) {
            tr = new Thread(() -> {
                try {
                    Thread.sleep(trigMs);
                    trig[0] = SystemClock.elapsedRealtimeNanos();
                    Runtime.getRuntime().exec(new String[]{"/system/bin/sh", "-c", cmd}).waitFor();
                } catch (Exception e) { e.printStackTrace(); }
            });
            tr.start();
        }
        int got = 0;
        while (got < total) {
            int r = ar.read(pcm, got, Math.min(4800, total - got));
            if (r <= 0) { System.err.println("read " + r); break; }
            got += r;
        }
        ar.stop();
        ar.release();
        if (tr != null) tr.join();
        // the first read returns audio captured from startRecording(); t0 is approximate (buffering latency)
        java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
        java.io.DataOutputStream d = new java.io.DataOutputStream(bo);
        String note = String.format("hkcap mic source=%d start_ns=%d trigger_ns=%d", src, t0, trig[0]);
        byte[] nb = note.getBytes();
        int listLen = 4 + 8 + nb.length + 1 + ((nb.length + 1) & 1);
        d.writeBytes("RIFF"); d.writeInt(Integer.reverseBytes(4 + 24 + 8 + listLen + 8 + got * 2)); d.writeBytes("WAVE");
        d.writeBytes("fmt "); d.writeInt(Integer.reverseBytes(16)); d.writeShort(Short.reverseBytes((short) 1));
        d.writeShort(Short.reverseBytes((short) 1)); d.writeInt(Integer.reverseBytes(fs)); d.writeInt(Integer.reverseBytes(fs * 2));
        d.writeShort(Short.reverseBytes((short) 2)); d.writeShort(Short.reverseBytes((short) 16));
        d.writeBytes("LIST"); d.writeInt(Integer.reverseBytes(listLen)); d.writeBytes("INFO"); d.writeBytes("ICMT");
        d.writeInt(Integer.reverseBytes(nb.length + 1)); d.write(nb); d.write(0); if (((nb.length + 1) & 1) == 1) d.write(0);
        d.writeBytes("data"); d.writeInt(Integer.reverseBytes(got * 2));
        for (int i = 0; i < got; i++) d.writeShort(Short.reverseBytes(pcm[i]));
        d.flush();
        try (FileOutputStream fo = new FileOutputStream(out)) { fo.write(bo.toByteArray()); }
        int peak = 0;
        for (int i = 0; i < got; i++) peak = Math.max(peak, Math.abs(pcm[i]));
        System.out.printf("%d frames @ %d Hz, peak %d, %s%n", got, fs, peak, note);
        System.exit(0);
    }

    public static void main(String[] args) throws Exception {
        String which = "accel", out = null, cmd = null;
        int periodUs = 0, durMs = 1500, trigMs = 500;
        boolean list = false;
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "-s": which = args[++i]; break;
                case "-p": periodUs = Integer.parseInt(args[++i]); break;
                case "-d": durMs = Integer.parseInt(args[++i]); break;
                case "-w": trigMs = Integer.parseInt(args[++i]); break;
                case "-c": cmd = args[++i]; break;
                case "-o": out = args[++i]; break;
                case "-l": list = true; break;
                default: System.err.println("unknown arg " + args[i]); System.exit(2);
            }
        }
        Looper.prepareMainLooper();
        Context ctx = systemContext();
        if (which.startsWith("mic")) {
            recordMic(which, durMs, trigMs, cmd, out);
            return;
        }
        SensorManager sm = (SensorManager) ctx.getSystemService(Context.SENSOR_SERVICE);
        if (list) {
            for (Sensor s : sm.getSensorList(Sensor.TYPE_ALL))
                System.out.printf("%d\t%s\t%s\tminDelay=%dus\tmaxDelay=%dus\tfifo=%d\twake=%b%n", s.getType(), s.getName(),
                        s.getVendor(), s.getMinDelay(), s.getMaxDelay(), s.getFifoMaxEventCount(), s.isWakeUpSensor());
            return;
        }
        int type = which.equals("accel") ? Sensor.TYPE_ACCELEROMETER
                : which.equals("uncal") ? Sensor.TYPE_ACCELEROMETER_UNCALIBRATED
                : which.equals("gyro") ? Sensor.TYPE_GYROSCOPE : Integer.parseInt(which);
        Sensor s = sm.getDefaultSensor(type);
        if (s == null) { System.err.println("no sensor of type " + type); System.exit(1); }
        int period = periodUs > 0 ? periodUs : s.getMinDelay();

        HandlerThread ht = new HandlerThread("hkcap");
        ht.start();
        SensorEventListener l = new SensorEventListener() {
            public void onSensorChanged(SensorEvent e) {
                int i = n;
                if (i >= CAP) return;
                ts[i] = e.timestamp;
                xyz[3 * i] = e.values[0];
                xyz[3 * i + 1] = e.values[1];
                xyz[3 * i + 2] = e.values[2];
                n = i + 1;
            }
            public void onAccuracyChanged(Sensor sensor, int accuracy) {}
        };
        if (!sm.registerListener(l, s, period, 0, new Handler(ht.getLooper()))) {
            System.err.println("registerListener failed");
            System.exit(1);
        }
        long t0 = SystemClock.elapsedRealtimeNanos();
        long trig = 0;
        if (cmd != null) {
            Thread.sleep(trigMs);
            trig = SystemClock.elapsedRealtimeNanos();
            Runtime.getRuntime().exec(new String[]{"/system/bin/sh", "-c", cmd}).waitFor();
            long rest = durMs - (SystemClock.elapsedRealtimeNanos() - t0) / 1000000;
            if (rest > 0) Thread.sleep(rest);
        } else {
            Thread.sleep(durMs);
        }
        sm.unregisterListener(l);
        ht.quitSafely();
        int cnt = n;
        PrintStream ps = out == null ? System.out : new PrintStream(new FileOutputStream(out));
        ps.printf("# hkcap sensor=\"%s\" vendor=\"%s\" type=%d period_us=%d min_delay_us=%d start_ns=%d trigger_ns=%d%n",
                s.getName(), s.getVendor(), type, period, s.getMinDelay(), t0, trig);
        ps.println("t_ns,x,y,z");
        for (int i = 0; i < cnt; i++)
            ps.printf("%d,%.6f,%.6f,%.6f%n", ts[i], xyz[3 * i], xyz[3 * i + 1], xyz[3 * i + 2]);
        ps.flush();
        if (out != null) {
            ps.close();
            double span = cnt > 1 ? (ts[cnt - 1] - ts[0]) / 1e9 : 0;
            System.out.printf("%d samples, %.3f s, %.1f Hz%n", cnt, span, span > 0 ? (cnt - 1) / span : 0);
        }
        System.exit(0);
    }
}
