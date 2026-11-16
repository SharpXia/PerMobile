package com.perfeval.fixture;

import android.os.SystemClock;
import android.util.Log;
import android.view.Choreographer;
import android.view.View;

/**
 * Jank 注入器（2026-11-06 体检 fixture-sustained-jank）。
 *
 * 两种模式（MainActivity / ScrollListActivity 共用，消除重复实现）：
 * - count 模式（既有行为，保持不变）：启动期一次性阻塞 count 帧，每帧 blockMs——
 *   注入随 count 耗尽即止，属启动期一次；
 * - sustained 模式（jank_duration_ms > 0 时启用，优先于 count）：持续窗口内
 *   Choreographer 逐帧回调，每帧阻塞 blockMs 后对 view postInvalidate 强制
 *   traversal（帧回调本身不触发渲染——实测仅 postFrameCallback 时 gfxinfo
 *   Total frames rendered 恒 1；必须逐帧 invalidate 才产生真实渲染帧）。
 *   blockMs=0 时为不阻塞的持续帧源（持续动画语料），>0 为持续 jank 语料——
 *   供 FPS/Jank 族的持续帧 golden 对拍，「静态 UI 出 0 帧属实」的局限由
 *   持续注入消除。
 */
public final class JankInjector {
    private static final String TAG = "PerfEval";

    private JankInjector() {
    }

    /** count 模式：阻塞 count 帧（每帧 blockMs），耗尽即止（启动期一次）。 */
    public static void scheduleCount(int blockMs, int count) {
        if (blockMs <= 0 || count <= 0) {
            return;
        }
        Choreographer.getInstance().postFrameCallback(new Choreographer.FrameCallback() {
            int remaining = count;

            @Override
            public void doFrame(long frameTimeNanos) {
                sleep(blockMs);
                remaining -= 1;
                if (remaining > 0) {
                    Choreographer.getInstance().postFrameCallback(this);
                }
            }
        });
    }

    /** sustained 模式：durationMs 窗口内逐帧渲染，每帧阻塞 blockMs（0=仅出帧）。 */
    public static void scheduleSustained(View view, int blockMs, int durationMs) {
        if (view == null || durationMs <= 0) {
            return;
        }
        final long deadline = SystemClock.uptimeMillis() + durationMs;
        Log.i(TAG, "PERFEVAL_EVENT {\"type\":\"metric\",\"name\":\"jank.sustained.start\",\"value\":"
                + durationMs + ",\"unit\":\"ms\",\"direction\":\"none\"}");
        Choreographer.getInstance().postFrameCallback(new Choreographer.FrameCallback() {
            @Override
            public void doFrame(long frameTimeNanos) {
                if (SystemClock.uptimeMillis() >= deadline) {
                    Log.i(TAG, "PERFEVAL_EVENT {\"type\":\"metric\",\"name\":\"jank.sustained.stop\","
                            + "\"value\":1,\"unit\":\"count\",\"direction\":\"none\"}");
                    return;
                }
                sleep(blockMs);
                // 帧回调不触发渲染，必须 invalidate 强制 traversal 才有真实渲染帧
                view.postInvalidate();
                Choreographer.getInstance().postFrameCallback(this);
            }
        });
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
