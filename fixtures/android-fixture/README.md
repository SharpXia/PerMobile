# PerfEval Android Fixture

Minimal Android fixture source tree for M2 validation.

This project is intentionally tiny and is meant to be opened with a local Android SDK or Gradle setup when you want to build an APK.

Build a debug APK:

```bash
./gradlew :app:assembleDebug
```

Use the generated APK with PerfEval:

```bash
perfeval run scenarios/android-fixture-startup.yaml \
  --android-device-run \
  --package-name com.perfeval.fixture \
  --apk fixtures/android-fixture/app/build/outputs/apk/debug/app-debug.apk \
  --activity .MainActivity
```

Supported launch extras:

- `startup_delay_ms`: sleep in `MainActivity.onCreate`.
- `main_thread_block_ms`: one main-thread block before first draw.
- `jank_block_ms` and `jank_count`: repeated frame callback blocking.
- `jank_duration_ms`: sustained injection window (ms; 2026-11-06
  fixture-sustained-jank). While > 0, Choreographer frames are produced
  continuously for the window and each frame blocks `jank_block_ms`
  (`jank_block_ms=0` keeps a continuous frame source without blocking, as a
  sustained-FPS corpus). Takes precedence over `jank_count`. Both
  `MainActivity` and `ScrollListActivity` support it.
- `memory_alloc_mb`: allocate and retain MB-sized byte arrays.
- `crash`: throw a runtime exception.

Example:

```bash
adb shell am start \
  -n com.perfeval.fixture/.MainActivity \
  --ei startup_delay_ms 300 \
  --ei jank_block_ms 80 \
  --ei jank_count 10 \
  --ei memory_alloc_mb 100

# 持续型注入（8s 窗口，每帧阻塞 40ms）：
adb shell am start \
  -n com.perfeval.fixture/.MainActivity \
  --ei jank_duration_ms 8000 \
  --ei jank_block_ms 40
```
