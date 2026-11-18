'use strict';

/**
 * PerfEval Desktop — preload（CommonJS，无构建步骤）。
 *
 * 只经 contextBridge 暴露 appVersion / platform 两个冻结值，无方法、无 IPC 通道：
 * 渲染层除这两个字段外拿不到任何 Node/Electron 能力。
 *
 * 应用版本由主进程随 BrowserWindow 参数传入。
 */

const { contextBridge } = require('electron');
const versionPrefix = '--perfeval-app-version=';
const version = process.argv.find((arg) => arg.startsWith(versionPrefix));

contextBridge.exposeInMainWorld('perfevalDesktop', {
  appVersion: version?.slice(versionPrefix.length) ?? '',
  platform: process.platform,
});
