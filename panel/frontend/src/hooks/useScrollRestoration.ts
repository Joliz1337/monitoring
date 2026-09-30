import { useLayoutEffect, type RefObject } from 'react'
import { useLocation } from 'react-router-dom'

const savedPositions = new Map<string, number>()

// Страница при возврате обычно ещё грузит данные и короче сохранённой позиции:
// докручиваем по мере роста контента, но не дольше этого срока
const RESTORE_TIMEOUT_MS = 10_000
const USER_INPUT_EVENTS = ['wheel', 'touchstart', 'keydown', 'mousedown'] as const

/**
 * Запоминает прокрутку каждой страницы и возвращает её при повторном заходе,
 * в том числе через меню, а не только кнопкой «Назад». Новая страница открывается сверху.
 */
export function useScrollRestoration(contentRef: RefObject<HTMLElement>) {
  const { pathname } = useLocation()

  // Своё восстановление браузер делает при «Назад»/«Вперёд» раньше, чем React
  // отрисует страницу, — позиция упирается в пустую страницу и сбивает нашу
  useLayoutEffect(() => {
    history.scrollRestoration = 'manual'
    return () => { history.scrollRestoration = 'auto' }
  }, [])

  // Layout-эффект, а не обычный: слушатель прокрутки должен смениться в том же
  // коммите, что и страница, иначе событие от укороченной новой страницы
  // записалось бы в позицию старой
  useLayoutEffect(() => {
    const content = contentRef.current
    if (!content) return

    const target = savedPositions.get(pathname) ?? 0
    let restoring = true
    let timer = 0

    const reachTarget = () => {
      window.scrollTo(0, target)
      return Math.abs(window.scrollY - target) < 1
    }
    const observer = new ResizeObserver(() => {
      if (reachTarget()) stopRestoring()
    })
    const stopRestoring = () => {
      restoring = false
      observer.disconnect()
      window.clearTimeout(timer)
      USER_INPUT_EVENTS.forEach(type => window.removeEventListener(type, stopRestoring))
    }
    const savePosition = () => {
      if (!restoring) savedPositions.set(pathname, window.scrollY)
    }

    window.addEventListener('scroll', savePosition, { passive: true })
    if (reachTarget()) {
      restoring = false
    } else {
      observer.observe(content)
      timer = window.setTimeout(stopRestoring, RESTORE_TIMEOUT_MS)
      // Пользователь начал крутить или кликать сам — страница не должна прыгать под ним
      USER_INPUT_EVENTS.forEach(type => window.addEventListener(type, stopRestoring, { passive: true }))
    }

    return () => {
      stopRestoring()
      window.removeEventListener('scroll', savePosition)
    }
  }, [pathname, contentRef])
}
