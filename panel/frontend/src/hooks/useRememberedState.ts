import { useEffect, useState, type Dispatch, type SetStateAction } from 'react'

// Живёт до перезагрузки вкладки браузера: раздел, открытый повторно, показывает
// то же, что было при уходе с него, — вкладку, выбранный профиль, период графика
const rememberedValues = new Map<string, unknown>()

export function useRememberedState<T>(key: string, initialValue: T): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() =>
    rememberedValues.has(key) ? rememberedValues.get(key) as T : initialValue
  )

  useEffect(() => { rememberedValues.set(key, value) }, [key, value])

  return [value, setValue]
}
