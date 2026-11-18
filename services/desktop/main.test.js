'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { resolveBackendCommand, resolveSidecarEnv } = require('./main');

test('Windows installer configures bundled ADB over an inherited override and uses the selected port', (t) => {
  const resourcesPath = fs.mkdtempSync(path.join(os.tmpdir(), 'PerfEval 测试 '));
  t.after(() => fs.rmSync(resourcesPath, { recursive: true, force: true }));
  const platformTools = path.join(resourcesPath, 'platform-tools');
  fs.mkdirSync(platformTools);
  const bundledAdb = path.join(platformTools, 'adb.exe');
  fs.writeFileSync(bundledAdb, '');

  const port = 49180;
  const inheritedPath = 'C:\\Windows\\System32';
  const overrides = resolveSidecarEnv({
    isPackaged: true,
    resourcesPath,
    port,
    platform: 'win32',
    environ: { Path: inheritedPath, PERFEVAL_ADB_PATH: 'C:\\Android\\adb.exe' },
  });

  assert.equal(overrides.PERFEVAL_ADB_PATH, bundledAdb);
  assert.equal(overrides.Path, `${platformTools};${inheritedPath}`);
  assert.equal(overrides.PERFEVAL_PARENT_STDIN, '1');
  assert.ok(overrides.PD_CORS_ORIGINS.split(',').includes(`http://127.0.0.1:${port}`));
  assert.deepEqual(resolveBackendCommand({ port }).args.slice(-2), ['--port', String(port)]);
});

test('Windows installer reports a missing ADB and Windows development keeps normal discovery', (t) => {
  const resourcesPath = fs.mkdtempSync(path.join(os.tmpdir(), 'PerfEval 测试 '));
  t.after(() => fs.rmSync(resourcesPath, { recursive: true, force: true }));
  assert.throws(
    () => resolveSidecarEnv({ isPackaged: true, resourcesPath, platform: 'win32' }),
    /安装目录缺少 ADB.*adb\.exe/,
  );
  const development = resolveSidecarEnv({ isPackaged: false, platform: 'win32' });
  assert.equal(development.PERFEVAL_ADB_PATH, undefined);
  assert.equal(development.PERFEVAL_PARENT_STDIN, undefined);
});
