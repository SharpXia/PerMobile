/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** live 模式下的默认后端地址（可被 URL 参数 pdbase 覆盖）；同源托管留空 */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
