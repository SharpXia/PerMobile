'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { Arch } = require('electron-builder');

function isWindowsX64(file) {
  const handle = fs.openSync(file, 'r');
  try {
    const dosHeader = Buffer.alloc(64);
    if (fs.readSync(handle, dosHeader, 0, dosHeader.length, 0) !== dosHeader.length ||
        dosHeader.toString('ascii', 0, 2) !== 'MZ') {
      return false;
    }
    const peHeader = Buffer.alloc(6);
    const peOffset = dosHeader.readUInt32LE(60);
    return fs.readSync(handle, peHeader, 0, peHeader.length, peOffset) === peHeader.length &&
      peHeader.readUInt32LE(0) === 0x4550 && peHeader.readUInt16LE(4) === 0x8664;
  } finally {
    fs.closeSync(handle);
  }
}

module.exports = function checkWindowsResources(context) {
  if (context.electronPlatformName !== 'win32') return;
  if (context.arch !== Arch.x64) throw new Error('PerfEval Desktop for Windows requires x64.');

  const staging = path.resolve(__dirname, '../../build/windows-electron/staging');
  const binaries = ['backend/perfeval-backend.exe', 'platform-tools/adb.exe'];
  const resources = [
    ...binaries,
    'backend/_internal/frontend-dist/index.html',
    'platform-tools/AdbWinApi.dll',
    'platform-tools/AdbWinUsbApi.dll',
    'platform-tools/licenses',
  ];
  for (const resource of resources) {
    if (!fs.existsSync(path.join(staging, resource))) {
      throw new Error(`Windows bundle is missing ${resource}. Run packaging/windows/build-electron.py first.`);
    }
  }
  for (const binary of binaries) {
    if (!isWindowsX64(path.join(staging, binary))) {
      throw new Error(`Windows bundle requires a Windows x64 executable at ${binary}.`);
    }
  }
};
