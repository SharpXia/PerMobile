# Reproducible PyInstaller build

This directory builds one native-platform onedir distribution containing the GUI and an independent `perfeval-cli` sidecar. Always build Windows artifacts on Windows and macOS artifacts on macOS.

## Prerequisites

- CPython 3.11.x.
- Exact versions from `requirements-build.txt`.
- A clean checkout for release candidates.

```bash
.venv/bin/python -m pip install -r packaging/pyinstaller/requirements-build.txt
.venv/bin/python packaging/pyinstaller/build.py --clean
```

The wrapper generates platform icons, version metadata, license staging, the native onedir artifact, a sorted zip, SHA256 values, and an automatic frozen smoke record. Normal builds use the actual build time so Finder and LaunchServices see a fresh application bundle. The wrapper recreates the archive and normalizes the `.app` tree, archive file, and ZIP entry timestamps to avoid stale creation dates or timezone offsets. Set `SOURCE_DATE_EPOCH` explicitly only when a fixed reproducible timestamp is required.

The metadata and zip ordering are deterministic inputs, but macOS's PyInstaller BUNDLE phase re-applies ad-hoc deep signatures/resource seals. Consecutive unsigned internal builds can therefore have different GUI/bundle hashes. Hash the final signed artifact and record the signing/timestamp source; see `packaging/build_records/` for the observed comparison.

## Runtime contract

- Development: configured CLI, then `perfeval` on PATH, then `sys.executable -m perfeval.cli.main`.
- Frozen macOS: `Contents/MacOS/perfeval-cli`.
- Frozen Windows: `PerfEval-Lite/perfeval-cli.exe`.
- Frozen startup requires `resources/version.json`, a runnable sidecar, and exact GUI/CLI version equality.
- User input is never passed through a shell.

## Smoke scope

The automatic smoke runs the sidecar `--version`, six synthetic cases in a Unicode/space path, ECharts resource generation, GUI offscreen startup, version metadata, licenses, and the Qt platform plugin with an isolated PATH and no `PYTHONHOME`/`PYTHONPATH`.

This is not a substitute for a clean-machine test. Windows 10/11, macOS Gatekeeper, signing, notarization, Defender, Intel/universal2, and certificate-backed release steps remain platform-specific `not_run` until executed and recorded.
