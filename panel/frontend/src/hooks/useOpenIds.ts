import { useCallback } from 'react'
import { useRememberedState } from './useRememberedState'

// Раскрытые карточки списка — сколько угодно разом. Последняя открытая идёт первой:
// страницы «список слева — профили справа» показывают её сверху, рядом со списком
export function useOpenIds<T extends number | string>(key: string) {
  const [openIds, setOpenIds] = useRememberedState<T[]>(key, [])

  const open = useCallback(
    (id: T) => setOpenIds(prev => (prev.includes(id) ? prev : [id, ...prev])),
    [setOpenIds],
  )
  const close = useCallback(
    (id: T) => setOpenIds(prev => prev.filter(openId => openId !== id)),
    [setOpenIds],
  )
  const toggle = useCallback(
    (id: T) => setOpenIds(prev => (prev.includes(id) ? prev.filter(openId => openId !== id) : [id, ...prev])),
    [setOpenIds],
  )

  return { openIds, setOpenIds, open, close, toggle }
}
