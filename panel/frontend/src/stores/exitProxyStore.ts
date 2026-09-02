import { create } from 'zustand'
import { toast } from 'sonner'
import i18n from '../i18n'
import {
  exitProxyApi,
  type ExitProxyBuiltinCheckKey,
  type ExitProxyChecks,
  type ExitProxyCustomCheckInput,
  type ExitProxyLogEntry,
  type ExitProxyNode,
  type ExitProxyNodePatch,
  type ExitProxySettings,
  type ExitProxySettingsPatch,
  type ExitProxySnippet,
  type ExitProxyStatus,
} from '../api/client'
import { extractError } from '../components/xraytest/useTestRun'

interface ExitProxyState {
  settings: ExitProxySettings | null
  status: ExitProxyStatus | null
  nodes: ExitProxyNode[]
  checks: ExitProxyChecks | null
  log: ExitProxyLogEntry[]
  snippet: ExitProxySnippet | null
  loaded: boolean
  busy: Record<string, true>

  fetchNodes: () => Promise<void>
  fetchStatus: () => Promise<void>
  fetchSettings: () => Promise<void>
  fetchChecks: () => Promise<void>
  fetchLog: (limit?: number) => Promise<void>
  fetchSnippet: () => Promise<void>
  fetchAll: () => Promise<void>

  saveSettings: (patch: ExitProxySettingsPatch) => Promise<boolean>
  updateNode: (serverId: number, patch: ExitProxyNodePatch, busyKey?: string) => Promise<boolean>
  checkNow: (serverId: number) => Promise<boolean>
  switchExit: (serverId: number, tag: string) => Promise<boolean>
  installWarp: (serverId: number) => Promise<string | null>
  setBuiltinCheck: (key: ExitProxyBuiltinCheckKey, enabled: boolean) => Promise<boolean>
  addCheck: (data: ExitProxyCustomCheckInput) => Promise<boolean>
  updateCheck: (id: string, data: ExitProxyCustomCheckInput) => Promise<boolean>
  deleteCheck: (id: string) => Promise<boolean>

  runAction: (key: string, fn: () => Promise<unknown>, okMsg?: string) => Promise<boolean>
  isBusy: (key: string) => boolean
  isNodeBusy: (serverId: number) => boolean
}

export const useExitProxyStore = create<ExitProxyState>((set, get) => {
  const replaceNode = (node: ExitProxyNode) =>
    set(state => ({ nodes: state.nodes.map(n => (n.server_id === node.server_id ? node : n)) }))

  return {
    settings: null,
    status: null,
    nodes: [],
    checks: null,
    log: [],
    snippet: null,
    loaded: false,
    busy: {},

    fetchNodes: async () => {
      try {
        const { data } = await exitProxyApi.getNodes()
        set({ nodes: data.nodes })
      } catch {
        // оставить прошлое состояние
      }
    },

    fetchStatus: async () => {
      try {
        const { data } = await exitProxyApi.getStatus()
        set({ status: data })
      } catch {
        // оставить прошлое состояние
      }
    },

    fetchSettings: async () => {
      try {
        const { data } = await exitProxyApi.getSettings()
        set({ settings: data })
      } catch {
        // оставить прошлое состояние
      }
    },

    fetchChecks: async () => {
      try {
        const { data } = await exitProxyApi.getChecks()
        set({ checks: data })
      } catch {
        // оставить прошлое состояние
      }
    },

    fetchLog: async (limit = 100) => {
      try {
        const { data } = await exitProxyApi.getLog(limit)
        set({ log: data.events })
      } catch {
        // оставить прошлое состояние
      }
    },

    fetchSnippet: async () => {
      try {
        const { data } = await exitProxyApi.getSnippet()
        set({ snippet: data })
      } catch {
        // блок покажет ошибку по пустому snippet
      }
    },

    fetchAll: async () => {
      await Promise.all([get().fetchSettings(), get().fetchStatus(), get().fetchNodes(), get().fetchChecks()])
      set({ loaded: true })
    },

    saveSettings: async (patch) => {
      return get().runAction('settings-save', async () => {
        const { data } = await exitProxyApi.updateSettings(patch)
        set({ settings: data })
        await get().fetchStatus()
      }, i18n.t('exit_proxy.saved'))
    },

    updateNode: async (serverId, patch, busyKey) => {
      return get().runAction(busyKey ?? `node-${serverId}-update`, async () => {
        const { data } = await exitProxyApi.updateNode(serverId, patch)
        replaceNode(data)
      })
    },

    checkNow: async (serverId) => {
      return get().runAction(`node-${serverId}-check`, async () => {
        const { data } = await exitProxyApi.checkNow(serverId)
        replaceNode(data)
      }, i18n.t('exit_proxy.check_done'))
    },

    switchExit: async (serverId, tag) => {
      return get().runAction(`node-${serverId}-switch`, async () => {
        const { data } = await exitProxyApi.switchExit(serverId, tag)
        replaceNode(data)
      }, i18n.t('exit_proxy.done'))
    },

    installWarp: async (serverId) => {
      let jobId: string | null = null
      await get().runAction(`node-${serverId}-warp`, async () => {
        const { data } = await exitProxyApi.installWarp(serverId)
        jobId = data.job_id
      }, i18n.t('exit_proxy.warp_install_started'))
      return jobId
    },

    setBuiltinCheck: async (key, enabled) => {
      return get().runAction(`builtin-${key}`, async () => {
        const { data } = await exitProxyApi.setBuiltinCheck(key, enabled)
        set({ checks: data })
      })
    },

    addCheck: async (data) => {
      return get().runAction('check-add', async () => {
        const { data: checks } = await exitProxyApi.addCheck(data)
        set({ checks })
      }, i18n.t('exit_proxy.check_added'))
    },

    updateCheck: async (id, data) => {
      return get().runAction(`check-${id}`, async () => {
        const { data: checks } = await exitProxyApi.updateCheck(id, data)
        set({ checks })
      }, i18n.t('exit_proxy.saved'))
    },

    deleteCheck: async (id) => {
      return get().runAction(`check-${id}`, async () => {
        const { data: checks } = await exitProxyApi.deleteCheck(id)
        set({ checks })
      }, i18n.t('exit_proxy.check_deleted'))
    },

    runAction: async (key, fn, okMsg) => {
      if (get().busy[key]) return false
      set(state => ({ busy: { ...state.busy, [key]: true } }))
      try {
        await fn()
        if (okMsg) toast.success(okMsg)
        return true
      } catch (e) {
        toast.error(extractError(e) || i18n.t('exit_proxy.action_failed'))
        return false
      } finally {
        set(state => {
          const next = { ...state.busy }
          delete next[key]
          return { busy: next }
        })
      }
    },

    isBusy: (key) => Boolean(get().busy[key]),
    isNodeBusy: (serverId) => Object.keys(get().busy).some(key => key.startsWith(`node-${serverId}-`)),
  }
})
