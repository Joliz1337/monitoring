import { create } from 'zustand'

interface CloudState {
    enabled: boolean
}

export const useCloudStore = create<CloudState>(() => ({
    enabled: false,
}))
