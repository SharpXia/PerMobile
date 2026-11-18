# macOS packaging

## PerfEval Desktop（Electron，Apple Silicon）

在搭载 Apple 芯片的 Mac 安装 CPython 3.14.10 和 Node.js 24.21.0，然后从仓库根目录构建：

```bash
python3.14 packaging/macos/build-electron.py
```

构建生成 `services/desktop/dist/mac-release/PerfEval-Desktop-<版本>-macos-arm64.dmg` 和 `.sha256`。脚本挂载 DMG，检查图标、ARM64 后端和代码签名，再用只含系统目录的 `PATH` 验证前端、后端和退出。用户运行应用时无需安装 Python 或 Node。连接 Android 设备时需要本机 ADB。

配置 Developer ID 和公证凭据后，构建验证已装订公证票据的应用。没有凭据时使用 ad-hoc 签名。 [GitHub 自动发布](../../services/desktop/README.md#github-自动发布)

## PerfEval Lite（PySide6 与 CLI）

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
