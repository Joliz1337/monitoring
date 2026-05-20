import { create } from 'zustand'
import type { FAQScreen } from '../components/FAQ/faq.types'

interface FAQState {
  isOpen: boolean
  screen: FAQScreen | null
  open: (screen: FAQScreen) => void
  close: () => void
}

export const useFAQStore = create<FAQState>((set) => ({
  isOpen: false,
  screen: null,
  open: (screen) => set({ isOpen: true, screen }),
  close: () => set({ isOpen: false }),
}))
