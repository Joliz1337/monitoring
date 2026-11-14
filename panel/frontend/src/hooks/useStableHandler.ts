import { useCallback, useRef } from 'react'

/**
 * Постоянная ссылка на обработчик, который пересоздаётся на каждом рендере. Memo-строкам
 * длинного списка нужна одна и та же функция между рендерами, иначе memo не срабатывает;
 * вызывается при этом всегда свежий обработчик, с актуальным состоянием.
 */
export function useStableHandler<Args extends unknown[]>(handler: (...args: Args) => unknown): (...args: Args) => void {
  const latest = useRef(handler)
  latest.current = handler
  return useCallback((...args: Args) => { latest.current(...args) }, [])
}
