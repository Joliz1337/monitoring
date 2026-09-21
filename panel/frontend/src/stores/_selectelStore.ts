import { create } from 'zustand'

interface SelectelState {
    enabled: boolean
}

export const useSelectelStore = create<SelectelState>(() => ({
    enabled: false,
}))
