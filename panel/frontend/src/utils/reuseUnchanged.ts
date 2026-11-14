const isPlainObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

/**
 * Свежий ответ опроса накладывается на прежнее состояние: всё, что не изменилось, остаётся
 * прежними объектами. Ответ без изменений возвращает прежнее состояние целиком — React
 * пропускает обновление; изменилась одна строка списка — memo-строки остальных не
 * перерисовываются. Рассчитано на данные из JSON: объекты, массивы и примитивы.
 */
export function reuseUnchanged<T>(prev: unknown, next: T): T {
  if (Array.isArray(prev) && Array.isArray(next)) {
    const merged = next.map((item, index) => reuseUnchanged(prev[index], item))
    const unchanged = prev.length === next.length && merged.every((item, index) => item === prev[index])
    return (unchanged ? prev : merged) as T
  }
  if (isPlainObject(prev) && isPlainObject(next)) {
    const keys = Object.keys(next)
    const merged: Record<string, unknown> = {}
    let unchanged = Object.keys(prev).length === keys.length
    for (const key of keys) {
      merged[key] = reuseUnchanged(prev[key], next[key])
      if (!(key in prev) || merged[key] !== prev[key]) unchanged = false
    }
    return (unchanged ? prev : merged) as T
  }
  return next
}
