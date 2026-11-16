# PerfEval Lite third-party notices

This directory is bundled with internal PyInstaller builds. It is a packaging aid, not a substitute for release legal review.

## Qt for Python / PySide6 / Shiboken6

- Version pinned for this build: 6.11.1.
- Declared wheel license: `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`.
- Project: <https://www.qt.io/qt-for-python>
- Source: <https://code.qt.io/cgit/pyside/pyside-setup.git/>
- The bundled build uses the LGPL-3.0 option; see `LGPL-3.0.txt` and the incorporated `GPL-3.0.txt` terms.

Recipients must be able to replace/relink the LGPL-covered Qt libraries. PerfEval Lite therefore uses an onedir layout and does not statically link or obfuscate Qt libraries.

## Apache ECharts

- The offline report bundle contains `echarts.min.js`.
- License: Apache-2.0; see `Apache-2.0.txt`.
- Project: <https://echarts.apache.org/>

## keyring

- Used by the desktop GUI to store LLM API keys in the operating system credential store.
- License: MIT.
- Project: <https://github.com/jaraco/keyring>
- Linux credential persistence may additionally depend on an operating-system Secret Service backend; when unavailable, PerfEval Lite keeps the key in memory for the current session only.

## Python

- The frozen application contains the CPython runtime used for the platform build.
- License: Python Software Foundation License Version 2.
- The build script copies the interpreter's `LICENSE` file into the packaged `licenses/Python` directory when the local Python distribution provides it.

## PyInstaller

- Version pinned for this build: 6.22.0.
- PyInstaller's bootloader and build tooling use GPL-2.0-or-later with the PyInstaller bootloader exception.
- The build script copies the installed PyInstaller `COPYING.txt` into `licenses/PyInstaller`.

## Project license status

The repository currently does not declare a top-level PerfEval Lite license. External distribution must remain blocked until the project owner supplies that license and completes a full Qt/plugin/transitive-dependency notice review.
