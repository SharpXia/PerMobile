# Windows packaging

## PerfEval Desktop（Electron，Windows x64）

安装包内含 Electron、冻结的 FastAPI/Python 后端、Vue 前端和 ADB 及其 DLL。用户打开应用后，可连接已开启 USB 调试并授权的 Android 手机。部分机型需通过 Windows Update 或手机厂商安装 USB 驱动。Python、Node 和完整 Android SDK 均无需由用户安装。若 Android Studio 或其他调试工具已占用手机的 USB 调试接口，先退出占用设备的工具，再重连手机并刷新设备列表。

在 Windows x64 构建机安装 **CPython 3.14.10 x64** 和 **Node.js 24.21.0 x64**，然后在仓库根目录运行：

```powershell
py -3.14 packaging/windows/build-electron.py
```

脚本按锁定版本安装构建依赖，构建前端，冻结后端，按 SHA-256 下载并核验 MSYS2 的开源 ADB 和依赖，把它们写入 NSIS 安装包。在仅含系统目录的 `PATH` 下验证桌面窗口、后端健康页、前端资源、ADB 设备枚举和退出，并生成：

```text
services/desktop/dist/windows-x64/PerfEval-Desktop-<version>-windows-x64-setup.exe
services/desktop/dist/windows-x64/PerfEval-Desktop-<version>-windows-x64-setup.exe.sha256
```

ADB 的许可证、源码和重建说明随包放在 `resources/platform-tools/licenses`。安装包按当前用户安装。升级或卸载时若提示应用正在运行，关闭窗口后重试。产物默认未签名，如有 Windows 代码签名证书，可配置 electron-builder 的 `WIN_CSC_LINK` 和 `WIN_CSC_KEY_PASSWORD`。

GitHub Actions 在 Windows runner 上静默安装到含中文和空格的路径，检查应用启动、ADB 和退出。两端构建均通过后，版本标签触发 GitHub Release。涉及 USB 采集的改动还需要在 Windows 实机上验证授权、采集和保存。 [桌面版发包流程](../../services/desktop/README.md#github-自动发布)

## PerfEval Lite（PySide6 与 CLI）

Build this configuration on Windows x64; PyInstaller does not cross-compile.
本仓库开发环境在 macOS，Windows 产物从未在本地构建过——以下命令与预期布局是
按 spec（`packaging/pyinstaller/perfeval.spec` 的 `win` 分支）推导的执行口径，
实际结果以 Windows 环境执行记录为准。

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r packaging\pyinstaller\requirements-build.txt
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python packaging\pyinstaller\build.py --clean
```

`build.py` 会在构建后自动执行 `packaging/pyinstaller/smoke.py`（sidecar `--version`、
6 个合成用例、离线路径、GUI offscreen 启动、Qt platform plugin、版本元数据与
licenses），并产出 sorted zip、SHA256 与 build-record。也可单独重跑：

```powershell
.venv\Scripts\python packaging\pyinstaller\smoke.py dist\PerfEval-Lite
```

CI 备用：`.github/workflows/windows-build.yml`（workflow_dispatch / v* tag 触发，
windows-latest + Python 3.11 + 固定依赖 → `build.py --clean` → 上传 zip）。
这是 Lite 的独立流程，Electron 发包使用上面的 Windows x64 Desktop workflow。

Expected layout:

```text
PerfEval-Lite\PerfEval-Lite.exe
PerfEval-Lite\perfeval-cli.exe
PerfEval-Lite\resources\version.json
PerfEval-Lite\licenses\...
```

## 版本号单一来源与同步点

| 位置 | 说明 |
| --- | --- |
| `perfeval/__init__.py` `__version__` | 唯一来源；CLI `--version`、manifest `toolVersion`、GUI 标题、打包 `version.json`（build.py 生成）都取自它 |
| `pyproject.toml` `version` | 与上一项保持一致（`pip install -e .` 生效） |
| `packaging/windows/version_info.txt` | Windows exe 资源版本（静态文件），升版本时必须与 `__version__` 同步修改（filevers/prodvers 四元组 + FileVersion/ProductVersion 字符串） |

Frozen 启动校验要求 GUI 与 sidecar 的版本完全一致（都读 `__version__`），改版本时
不要只改其中一处。

Required Windows validation remains `not_run` outside Windows: Windows 10/11 startup, Chinese user/install paths, Defender behavior, terminate/kill semantics, and Authenticode signing. A formal release should sign both executables and all applicable nested binaries before creating the zip/installer.
