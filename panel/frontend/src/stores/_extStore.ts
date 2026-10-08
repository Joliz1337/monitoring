import { create } from 'zustand'

interface NavItem {
    path: string
    icon: string
    label: string
}

interface ExtState {
    enabled: boolean
    navItems: NavItem[]
}

export const useExtStore = create<ExtState>(() => ({
    enabled: false,
    navItems: [],
}))
