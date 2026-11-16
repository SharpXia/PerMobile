package com.perfeval.fixture;

import android.app.Activity;
import android.os.Bundle;
import android.os.SystemClock;
import android.util.Log;
import android.view.Gravity;
import android.widget.TextView;
import java.util.ArrayList;
import java.util.List;

public final class MainActivity extends Activity {
    private static final List<byte[]> HELD_MEMORY = new ArrayList<>();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        long start = SystemClock.uptimeMillis();
        int startupDelayMs = getIntent().getIntExtra("startup_delay_ms", 0);
        int mainThreadBlockMs = getIntent().getIntExtra("main_thread_block_ms", 0);
        int jankBlockMs = getIntent().getIntExtra("jank_block_ms", 0);
        int jankCount = getIntent().getIntExtra("jank_count", 0);
        // 2026-11-06 体检 fixture-sustained-jank：jank_duration_ms > 0 走持续注入
        //（窗口内逐帧回调，每帧阻塞 jank_block_ms），优先于一次性 jank_count。
        int jankDurationMs = getIntent().getIntExtra("jank_duration_ms", 0);
        int memoryAllocMb = getIntent().getIntExtra("memory_alloc_mb", 0);
        boolean crash = getIntent().getBooleanExtra("crash", false);

        sleep(startupDelayMs);
        allocateMemory(memoryAllocMb);
        sleep(mainThreadBlockMs);
        if (crash) {
            throw new RuntimeException("PerfEval fixture requested crash");
        }

        super.onCreate(savedInstanceState);
        TextView text = new TextView(this);
        text.setGravity(Gravity.CENTER);
        text.setTextSize(20);
        long startupMs = SystemClock.uptimeMillis() - start;
        text.setText("PerfEval Fixture\nstartupMs=" + startupMs);
        setContentView(text);
        Log.i("PerfEval", "PERFEVAL_EVENT {\"type\":\"metric\",\"name\":\"startup.cold.ms\",\"value\":"
                + startupMs + ",\"unit\":\"ms\",\"direction\":\"lower_better\"}");
        if (jankDurationMs > 0) {
            JankInjector.scheduleSustained(text, jankBlockMs, jankDurationMs);
        } else {
            JankInjector.scheduleCount(jankBlockMs, jankCount);
        }
    }

    private static void allocateMemory(int memoryAllocMb) {
        if (memoryAllocMb <= 0) {
            return;
        }
        for (int index = 0; index < memoryAllocMb; index++) {
            HELD_MEMORY.add(new byte[1024 * 1024]);
        }
    }

    private static void sleep(int durationMs) {
        if (durationMs <= 0) {
            return;
        }
        try {
            Thread.sleep(durationMs);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
    }
}

