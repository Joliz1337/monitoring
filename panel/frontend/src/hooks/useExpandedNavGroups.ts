import { useCallback, useState } from 'react'
import { readStorage, writeStorage } from '../utils/storage'
import type { NavGroupId } from '../config/modules'

const STORAGE_KEY = 'sidebar_expanded_groups'

function readExpanded(): Set<string> {
  try {
    const saved: unknown = JSON.parse(readStorage(STORAGE_KEY) || '[]')
    return new Set(Array.isArray(saved) ? saved : [])
  } catch {
    return new Set()
  }
}

/** Раскрытые папки бокового меню; без сохранённого состояния все свёрнуты */
export function useExpandedNavGroups(): [Set<string>, (groupId: NavGroupId, open: boolean) => void] {
  const [expanded, setExpanded] = useState<Set<string>>(readExpanded)

  const setGroupOpen = useCallback((groupId: NavGroupId, open: boolean) => {
    setExpanded(prev => {
      if (prev.has(groupId) === open) return prev
      const next = new Set(prev)
      if (open) next.add(groupId)
      else next.delete(groupId)
      writeStorage(STORAGE_KEY, JSON.stringify([...next]))
      return next
    })
  }, [])

  return [expanded, setGroupOpen]
}
