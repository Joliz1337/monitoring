import { create } from 'zustand'
import { infraApi, InfraTree } from '../api/client'

interface InfraState {
  tree: InfraTree | null
  isLoading: boolean

  fetchTree: () => Promise<void>
  createAccount: (name: string) => Promise<void>
  updateAccount: (id: number, name: string) => Promise<void>
  deleteAccount: (id: number) => Promise<void>
  createProject: (accountId: number, name: string) => Promise<void>
  updateProject: (id: number, data: { name?: string; account_id?: number }) => Promise<void>
  deleteProject: (id: number) => Promise<void>
  addServerToProject: (projectId: number, serverId: number) => Promise<void>
  removeServerFromProject: (projectId: number, serverId: number) => Promise<void>
}

export const useInfraStore = create<InfraState>((set, get) => ({
  tree: null,
  isLoading: false,

  fetchTree: async () => {
    set({ isLoading: true })
    try {
      const { data } = await infraApi.getTree()
      set({ tree: data, isLoading: false })
    } catch {
      set({ isLoading: false })
    }
  },

  createAccount: async (name) => {
    const { data } = await infraApi.createAccount(name)
    const tree = get().tree
    if (tree && data.account) {
      set({ tree: { ...tree, accounts: [...tree.accounts, data.account] } })
    }
  },

  updateAccount: async (id, name) => {
    await infraApi.updateAccount(id, name)
    const tree = get().tree
    if (!tree) return
    set({
      tree: {
        ...tree,
        accounts: tree.accounts.map(a => a.id === id ? { ...a, name } : a),
      },
    })
  },

  deleteAccount: async (id) => {
    await infraApi.deleteAccount(id)
    const tree = get().tree
    if (!tree) return
    const account = tree.accounts.find(a => a.id === id)
    const freedServerIds = account?.projects.flatMap(p => p.server_ids) ?? []
    set({
      tree: {
        ...tree,
        accounts: tree.accounts.filter(a => a.id !== id),
        unassigned_server_ids: [...tree.unassigned_server_ids, ...freedServerIds].sort((a, b) => a - b),
      },
    })
  },

  createProject: async (accountId, name) => {
    const { data } = await infraApi.createProject(accountId, name)
    const tree = get().tree
    if (!tree || !data.project) return
    set({
      tree: {
        ...tree,
        accounts: tree.accounts.map(a =>
          a.id === accountId ? { ...a, projects: [...a.projects, data.project] } : a,
        ),
      },
    })
  },

  updateProject: async (id, data) => {
    await infraApi.updateProject(id, data)
    const tree = get().tree
    if (!tree) return
    set({
      tree: {
        ...tree,
        accounts: tree.accounts.map(a => ({
          ...a,
          projects: a.projects.map(p => p.id === id ? { ...p, ...data } : p),
        })),
      },
    })
  },

  deleteProject: async (id) => {
    await infraApi.deleteProject(id)
    const tree = get().tree
    if (!tree) return
    let freedServerIds: number[] = []
    const accounts = tree.accounts.map(a => {
      const proj = a.projects.find(p => p.id === id)
      if (proj) freedServerIds = proj.server_ids
      return { ...a, projects: a.projects.filter(p => p.id !== id) }
    })
    set({
      tree: {
        ...tree,
        accounts,
        unassigned_server_ids: [...tree.unassigned_server_ids, ...freedServerIds].sort((a, b) => a - b),
      },
    })
  },

  addServerToProject: async (projectId, serverId) => {
    await infraApi.addServerToProject(projectId, serverId)
    const tree = get().tree
    if (!tree) return
    set({
      tree: {
        ...tree,
        accounts: tree.accounts.map(a => ({
          ...a,
          projects: a.projects.map(p =>
            p.id === projectId ? { ...p, server_ids: [...p.server_ids, serverId] } : p,
          ),
        })),
        unassigned_server_ids: tree.unassigned_server_ids.filter(id => id !== serverId),
      },
    })
  },

  removeServerFromProject: async (projectId, serverId) => {
    await infraApi.removeServerFromProject(projectId, serverId)
    const tree = get().tree
    if (!tree) return
    set({
      tree: {
        ...tree,
        accounts: tree.accounts.map(a => ({
          ...a,
          projects: a.projects.map(p =>
            p.id === projectId ? { ...p, server_ids: p.server_ids.filter(id => id !== serverId) } : p,
          ),
        })),
        unassigned_server_ids: [...tree.unassigned_server_ids, serverId].sort((a, b) => a - b),
      },
    })
  },
}))
