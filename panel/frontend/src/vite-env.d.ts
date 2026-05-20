/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_PANEL_UID?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
