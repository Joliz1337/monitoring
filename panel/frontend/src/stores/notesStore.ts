import { create } from 'zustand'
import { notesApi, SharedTask } from '../api/client'

interface NotesState {
  content: string
  version: number
  tasks: SharedTask[]
  isOpen: boolean
  isLoading: boolean
  isSaving: boolean
  isConnected: boolean
  _lastSentVersion: number
  _debounceTimer: ReturnType<typeof setTimeout> | null
  _abortController: AbortController | null

  toggle: () => void
  open: () => void
  close: () => void
  setContent: (content: string) => void
  loadContent: () => Promise<void>
  loadTasks: () => Promise<void>
  createTask: (text: string) => Promise<void>
  toggleTask: (id: number, isDone: boolean) => Promise<void>
  deleteTask: (id: number) => Promise<void>
  connectSSE: () => void
  disconnectSSE: () => void
}

export const useNotesStore = create<NotesState>((set, get) => ({
  content: '',
  version: 0,
  tasks: [],
  isOpen: false,
  isLoading: false,
  isSaving: false,
  isConnected: false,
  _lastSentVersion: 0,
  _debounceTimer: null,
  _abortController: null,

  toggle: () => {
    if (get().isOpen) get().close()
    else get().open()
  },

  open: () => {
    set({ isOpen: true })
    get().loadContent()
    get().loadTasks()
    get().connectSSE()
  },

  close: () => {
    set({ isOpen: false })
    get().disconnectSSE()
  },

  setContent: (content: string) => {
    const { version, _debounceTimer } = get()
    set({ content })

    if (_debounceTimer) clearTimeout(_debounceTimer)

    const timer = setTimeout(async () => {
      set({ isSaving: true })
      try {
        const { data } = await notesApi.saveContent(content, version)
        if (data.status === 'ok') {
          set({ version: data.version, _lastSentVersion: data.version })
        } else if (data.status === 'conflict' && data.content != null) {
          set({ content: data.content, version: data.version })
        }
      } catch {
        // retry on next edit
      } finally {
        set({ isSaving: false })
      }
    }, 500)

    set({ _debounceTimer: timer })
  },

  loadContent: async () => {
    set({ isLoading: true })
    try {
      const { data } = await notesApi.getContent()
      set({ content: data.content, version: data.version, isLoading: false })
    } catch {
      set({ isLoading: false })
    }
  },

  loadTasks: async () => {
    try {
      const { data } = await notesApi.getTasks()
      set({ tasks: data.tasks })
    } catch { /* ignore */ }
  },

  createTask: async (text: string) => {
    try {
      const { data } = await notesApi.createTask(text)
      set({ tasks: data.tasks })
    } catch { /* ignore */ }
  },

  toggleTask: async (id: number, isDone: boolean) => {
    set(s => ({ tasks: s.tasks.map(t => t.id === id ? { ...t, is_done: isDone } : t) }))
    try {
      const { data } = await notesApi.toggleTask(id, isDone)
      set({ tasks: data.tasks })
    } catch { /* ignore */ }
  },

  deleteTask: async (id: number) => {
    set(s => ({ tasks: s.tasks.filter(t => t.id !== id) }))
    try {
      const { data } = await notesApi.deleteTask(id)
      set({ tasks: data.tasks })
    } catch { /* ignore */ }
  },

  connectSSE: () => {
    const controller = new AbortController()
    set({ _abortController: controller })

    const connect = async () => {
      try {
        const res = await fetch(notesApi.getStreamUrl(), {
          credentials: 'include',
          signal: controller.signal,
          headers: { Accept: 'text/event-stream' },
        })

        if (!res.ok || !res.body) {
          set({ isConnected: false })
          return
        }

        set({ isConnected: true })
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lastNL = buffer.lastIndexOf('\n')
          if (lastNL === -1) continue

          const complete = buffer.substring(0, lastNL)
          buffer = buffer.substring(lastNL + 1)

          let eventType = ''
          let eventData = ''

          for (const line of complete.split('\n')) {
            if (line.startsWith('event: ')) {
              eventType = line.slice(7).trim()
            } else if (line.startsWith('data: ')) {
              eventData = line.slice(6)
            } else if (line.trim() === '' && eventType && eventData) {
              if (eventType === 'update') {
                try {
                  const parsed = JSON.parse(eventData)
                  if (parsed.type === 'note_update' && parsed.version !== get()._lastSentVersion) {
                    set({ content: parsed.content, version: parsed.version })
                  } else if (parsed.type === 'tasks_changed') {
                    set({ tasks: parsed.tasks })
                  }
                } catch { /* ignore */ }
              }
              eventType = ''
              eventData = ''
            }
          }
        }
      } catch (err) {
        if ((err as Error).name !== 'AbortError') {
          set({ isConnected: false })
          setTimeout(() => {
            if (get().isOpen && !controller.signal.aborted) connect()
          }, 3000)
        }
      }
    }

    connect()
  },

  disconnectSSE: () => {
    const { _abortController, _debounceTimer } = get()
    _abortController?.abort()
    if (_debounceTimer) clearTimeout(_debounceTimer)
    set({ isConnected: false, _abortController: null, _debounceTimer: null })
  },
}))
