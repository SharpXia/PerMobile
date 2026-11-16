'use strict';

/**
 * PerfEval Desktop — preload（CommonJS，无构建步骤）。
 *
 * 只经 contextBridge 暴露 appVersion / platform 两个冻结值，无方法、无 IPC 通道：
 * 渲染层除这两个字段外拿不到任何 Node/Electron 能力。
 *
 * 主进程 webPreferences.sandbox=false 是配套选择：sandboxed preload 的 electron
 * 模块子集不含 app，取不到 app.getVersion()；contextIsolation=true 与
 * nodeIntegration=false 维持不变（见 main.js 文件头安全模型注释）。
 */

const { contextBridge, app } = require('electron');

contextBridge.exposeInMainWorld('perfevalDesktop', {
  appVersion: app.getVersion(),
  platform: process.platform,
});
