# macOS packaging

Build on macOS; PyInstaller does not cross-compile. When a valid code-signing identity is available, the build automatically prefers Developer ID Application, Apple Development, then Mac App Store application identities. The selected certificate fingerprint is cached in the ignored local file `.perfeval/codesign-identity`, so later builds keep using the same application identity even when multiple certificates are installed. After PyInstaller creates the bundle, the local build signs the complete app with that stable identity and no online timestamp. This keeps the application's macOS Keychain identity consistent across local rebuilds. Without a usable identity the build falls back to ad-hoc signing. Use `--adhoc-sign` to force that fallback, or `--codesign-identity <name-or-sha1>` / `PERFEVAL_CODESIGN_IDENTITY` to select an identity explicitly.

```bash
.venv/bin/python packaging/pyinstaller/build.py --clean
.venv/bin/python packaging/pyinstaller/smoke.py "dist/PerfEval Lite.app"
```

Expected layout:

```text
PerfEval Lite.app/Contents/MacOS/PerfEval-Lite
PerfEval Lite.app/Contents/MacOS/perfeval-cli
PerfEval Lite.app/Contents/Frameworks/...
PerfEval Lite.app/Contents/Resources/licenses/...
```

Release signing with Developer ID must still be notarized, stapled, and smoke-tested on another machine. Apple Development signing is suitable for stable local Keychain access but is not a replacement for Developer ID distribution signing.
