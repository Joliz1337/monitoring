import { create } from 'zustand'

interface YcState {
    enabled: boolean
}

export const useYcStore = create<YcState>(() => ({
    enabled: false,
}))
