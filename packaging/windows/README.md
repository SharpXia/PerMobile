# Windows packaging

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
仓库当前无 remote，该文件从未被执行过。

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
