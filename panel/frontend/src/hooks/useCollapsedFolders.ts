import { useCallback, useState } from 'react'
import { writeStorage } from '../utils/storage'

function readCollapsed(storageKey: string): Set<string> {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(storageKey) || '[]')
    return new Set(Array.isArray(saved) ? saved : [])
  } catch {
    return new Set()
  }
}

/** Свёрнутые папки страницы; у каждой страницы свой ключ localStorage */
export function useCollapsedFolders(storageKey: string): [Set<string>, (folder: string) => void] {
  const [collapsed, setCollapsed] = useState<Set<string>>(() => readCollapsed(storageKey))

  const toggle = useCallback((folder: string) => {
    setCollapsed(prev => {
      const next = new Set(prev)
      if (next.has(folder)) next.delete(folder)
      else next.add(folder)
      writeStorage(storageKey, JSON.stringify([...next]))
      return next
    })
  }, [storageKey])

  return [collapsed, toggle]
}
