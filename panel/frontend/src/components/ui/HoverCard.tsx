import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type MouseEvent,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'

interface HoverCardProps {
  children: ReactNode
  // Рендерится только пока окно открыто: содержимое само грузит данные при монтировании
  content: () => ReactNode
  openDelay?: number
  closeDelay?: number
  offset?: number
  className?: string
  triggerClassName?: string
}

interface Coords {
  top: number
  left: number
}

const VIEWPORT_PAD = 8

const canHover = () =>
  typeof window !== 'undefined' &&
  window.matchMedia?.('(hover: hover)').matches !== false

/**
 * Окно по наведению, в которое можно завести курсор и кликнуть — в отличие от Tooltip.
 * Курсор, ушедший с триггера на само окно, его не закрывает (закрытие отложено на
 * closeDelay). На тач-устройствах наведения нет — окно открывает и закрывает тап.
 */
export function HoverCard({
  children,
  content,
  openDelay = 250,
  closeDelay = 150,
  offset = 8,
  className = '',
  triggerClassName = '',
}: HoverCardProps) {
  const [open, setOpen] = useState(false)
  const [coords, setCoords] = useState<Coords | null>(null)
  const triggerRef = useRef<HTMLDivElement | null>(null)
  const cardRef = useRef<HTMLDivElement | null>(null)
  const timer = useRef<number | null>(null)

  const clearTimer = useCallback(() => {
    if (timer.current) {
      window.clearTimeout(timer.current)
      timer.current = null
    }
  }, [])

  const scheduleOpen = useCallback((next: boolean, delay: number) => {
    clearTimer()
    timer.current = window.setTimeout(() => setOpen(next), delay)
  }, [clearTimer])

  // Координаты при закрытии не сбрасываются: окно доигрывает анимацию исчезновения на месте
  useLayoutEffect(() => {
    if (!open) return
    const trigger = triggerRef.current
    const card = cardRef.current
    if (!trigger || !card) return

    // Окно под триггером, а если снизу не влезает — над ним
    const compute = () => {
      const rect = trigger.getBoundingClientRect()
      const width = card.offsetWidth
      const height = card.offsetHeight
      const below = rect.bottom + offset
      const above = rect.top - offset - height
      const fitsBelow = below + height <= window.innerHeight - VIEWPORT_PAD
      const top = fitsBelow || above < VIEWPORT_PAD ? below : above
      const left = Math.min(rect.left, window.innerWidth - width - VIEWPORT_PAD)
      setCoords({ top: Math.max(VIEWPORT_PAD, top), left: Math.max(VIEWPORT_PAD, left) })
    }

    compute()
    // Высота меняется, когда приходят данные, — позицию пересчитываем вслед за ней
    const observer = new ResizeObserver(compute)
    observer.observe(card)
    window.addEventListener('scroll', compute, true)
    window.addEventListener('resize', compute)
    return () => {
      observer.disconnect()
      window.removeEventListener('scroll', compute, true)
      window.removeEventListener('resize', compute)
    }
  }, [open, offset])

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (cardRef.current?.contains(target) || triggerRef.current?.contains(target)) return
      setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  useEffect(() => clearTimer, [clearTimer])

  const handleTriggerClick = (event: MouseEvent) => {
    if (canHover()) return
    // Клик по триггеру внутри кликабельной карточки открыл бы её страницу
    event.stopPropagation()
    setOpen(prev => !prev)
  }

  return (
    <>
      <div
        ref={triggerRef}
        className={triggerClassName}
        onMouseEnter={() => canHover() && scheduleOpen(true, openDelay)}
        onMouseLeave={() => canHover() && scheduleOpen(false, closeDelay)}
        onClick={handleTriggerClick}
      >
        {children}
      </div>
      {typeof document !== 'undefined' &&
        createPortal(
          <AnimatePresence>
            {open && (
              <motion.div
                ref={cardRef}
                role="dialog"
                initial={{ opacity: 0, y: -4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={{ duration: 0.15, ease: 'easeOut' }}
                style={{
                  position: 'fixed',
                  top: coords?.top ?? -9999,
                  left: coords?.left ?? -9999,
                  zIndex: 9998,
                }}
                onMouseEnter={() => canHover() && clearTimer()}
                onMouseLeave={() => canHover() && scheduleOpen(false, closeDelay)}
                // Портал всплывает по дереву React: без остановки клик внутри окна
                // дошёл бы до карточки-родителя и открыл её страницу
                onClick={event => event.stopPropagation()}
                className={`rounded-xl bg-dark-800 border border-dark-700 shadow-xl shadow-black/40 ${className}`}
              >
                {content()}
              </motion.div>
            )}
          </AnimatePresence>,
          document.body
        )}
    </>
  )
}
