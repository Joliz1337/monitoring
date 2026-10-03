import { create } from 'zustand'
import { systemApi, UpdateSummary } from '../api/client'

interface UpdateSummaryState {
  summary: UpdateSummary | null
  refresh: () => Promise<void>
}

export const useUpdateSummaryStore = create<UpdateSummaryState>(set => ({
  summary: null,
  refresh: async () => {
    try {
      const { data } = await systemApi.getUpdateSummary()
      set({ summary: data })
    } catch {
      // значок — только подсказка: без сводки меню остаётся как было
    }
  },
}))
