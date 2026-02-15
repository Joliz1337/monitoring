import { create } from 'zustand'

interface NavItem {
    path: string
    icon: string
    label: string
}

interface ExtState {
    enabled: boolean
    navItem: NavItem | null
}

export const useExtStore = create<ExtState>(() => ({
    enabled: false,
    navItem: null,
}))
