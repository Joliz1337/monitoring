import {
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'

type Side = 'top' | 'bottom' | 'left' | 'right'

interface TooltipProps {
  label: ReactNode
  position?: Side
  withArrow?: boolean
  openDelay?: number
  closeDelay?: number
  disabled?: boolean
  offset?: number
  children: ReactElement
  className?: string
  maxWidth?: number
}

interface Coords {
  top: number
  left: number
  side: Side
}

const canHover = () =>
  typeof window !== 'undefined' &&
  window.matchMedia?.('(hover: hover)').matches !== false

export function Tooltip({
  label,
  position = 'top',
  withArrow = true,
  openDelay = 300,
  closeDelay = 0,
  disabled = false,
  offset = 8,
  children,
  className = '',
  maxWidth = 260,
}: TooltipProps) {
  const [open, setOpen] = useState(false)
  const [coords, setCoords] = useState<Coords | null>(null)
  const triggerRef = useRef<HTMLElement | null>(null)
  const tooltipRef = useRef<HTMLDivElement | null>(null)
  const openTimer = useRef<number | null>(null)
  const closeTimer = useRef<number | null>(null)
  const tooltipId = useId()

  const clearTimers = useCallback(() => {
    if (openTimer.current) {
      window.clearTimeout(openTimer.current)
      openTimer.current = null
    }
    if (closeTimer.current) {
      window.clearTimeout(closeTimer.current)
      closeTimer.current = null
    }
  }, [])

  const show = useCallback(() => {
    if (disabled || !label) return
    if (!canHover()) return
    clearTimers()
    openTimer.current = window.setTimeout(() => setOpen(true), openDelay)
  }, [disabled, label, openDelay, clearTimers])

  const hide = useCallback(() => {
    clearTimers()
    if (closeDelay > 0) {
      closeTimer.current = window.setTimeout(() => setOpen(false), closeDelay)
    } else {
      setOpen(false)
    }
  }, [closeDelay, clearTimers])

  const showImmediate = useCallback(() => {
    if (disabled || !label) return
    clearTimers()
    setOpen(true)
  }, [disabled, label, clearTimers])

  useLayoutEffect(() => {
    if (!open || !triggerRef.current || !tooltipRef.current) return

    const compute = () => {
      if (!triggerRef.current || !tooltipRef.current) return
      const trigger = triggerRef.current.getBoundingClientRect()
      const tipWidth = tooltipRef.current.offsetWidth
      const tipHeight = tooltipRef.current.offsetHeight
      const vw = window.innerWidth
      const vh = window.innerHeight
      const pad = 8

      let side: Side = position
      if (side === 'top' && trigger.top - tipHeight - offset < pad) side = 'bottom'
      else if (side === 'bottom' && trigger.bottom + tipHeight + offset > vh - pad) side = 'top'
      else if (side === 'left' && trigger.left - tipWidth - offset < pad) side = 'right'
      else if (side === 'right' && trigger.right + tipWidth + offset > vw - pad) side = 'left'

      let top = 0
      let left = 0
      if (side === 'top') {
        top = trigger.top - tipHeight - offset
        left = trigger.left + trigger.width / 2 - tipWidth / 2
      } else if (side === 'bottom') {
        top = trigger.bottom + offset
        left = trigger.left + trigger.width / 2 - tipWidth / 2
      } else if (side === 'left') {
        top = trigger.top + trigger.height / 2 - tipHeight / 2
        left = trigger.left - tipWidth - offset
      } else {
        top = trigger.top + trigger.height / 2 - tipHeight / 2
        left = trigger.right + offset
      }

      left = Math.max(pad, Math.min(left, vw - tipWidth - pad))
      top = Math.max(pad, Math.min(top, vh - tipHeight - pad))

      setCoords({ top, left, side })
    }

    compute()
    window.addEventListener('scroll', compute, true)
    window.addEventListener('resize', compute)
    return () => {
      window.removeEventListener('scroll', compute, true)
      window.removeEventListener('resize', compute)
    }
  }, [open, position, offset, label])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  useEffect(() => () => clearTimers(), [clearTimers])

  if (!isValidElement(children)) return children

  const childProps = (children as ReactElement<Record<string, unknown>>).props

  const setTriggerRef = (node: HTMLElement | null) => {
    triggerRef.current = node
    const existingRef = (children as unknown as { ref?: unknown }).ref
    if (typeof existingRef === 'function') {
      (existingRef as (n: HTMLElement | null) => void)(node)
    } else if (existingRef && typeof existingRef === 'object' && 'current' in existingRef) {
      (existingRef as { current: HTMLElement | null }).current = node
    }
  }

  const trigger = cloneElement(children, {
    ref: setTriggerRef,
    onMouseEnter: (e: React.MouseEvent) => {
      (childProps.onMouseEnter as ((e: React.MouseEvent) => void) | undefined)?.(e)
      show()
    },
    onMouseLeave: (e: React.MouseEvent) => {
      (childProps.onMouseLeave as ((e: React.MouseEvent) => void) | undefined)?.(e)
      hide()
    },
    onFocus: (e: React.FocusEvent) => {
      (childProps.onFocus as ((e: React.FocusEvent) => void) | undefined)?.(e)
      showImmediate()
    },
    onBlur: (e: React.FocusEvent) => {
      (childProps.onBlur as ((e: React.FocusEvent) => void) | undefined)?.(e)
      setOpen(false)
    },
    'aria-describedby': open ? tooltipId : (childProps['aria-describedby'] as string | undefined),
  } as Record<string, unknown>)

  const variants = {
    initial: { opacity: 0, scaleX: 0.6 },
    animate: { opacity: 1, scaleX: 1 },
    exit: { opacity: 0, scaleX: 0.6 },
  }

  const transformOrigin = (side: Side | undefined) => {
    switch (side) {
      case 'left': return 'right center'
      case 'right': return 'left center'
      case 'bottom': return 'center top'
      default: return 'center bottom'
    }
  }

  const arrowClasses = (side: Side) => {
    const base = 'absolute w-2 h-2 rotate-45 bg-dark-800 border-dark-700'
    switch (side) {
      case 'top':
        return `${base} left-1/2 -translate-x-1/2 -bottom-[5px] border-r border-b`
      case 'bottom':
        return `${base} left-1/2 -translate-x-1/2 -top-[5px] border-l border-t`
      case 'left':
        return `${base} top-1/2 -translate-y-1/2 -right-[5px] border-t border-r`
      case 'right':
        return `${base} top-1/2 -translate-y-1/2 -left-[5px] border-b border-l`
    }
  }

  return (
    <>
      {trigger}
      {typeof document !== 'undefined' &&
        createPortal(
          <AnimatePresence>
            {open && (
              <motion.div
                ref={tooltipRef}
                id={tooltipId}
                role="tooltip"
                variants={variants}
                initial="initial"
                animate="animate"
                exit="exit"
                transition={{ duration: 0.15, ease: 'easeOut' }}
                style={{
                  position: 'fixed',
                  top: coords?.top ?? -9999,
                  left: coords?.left ?? -9999,
                  maxWidth,
                  transformOrigin: transformOrigin(coords?.side),
                  pointerEvents: 'none',
                  zIndex: 9999,
                }}
                className={`rounded-md px-2.5 py-1.5 text-xs font-medium bg-dark-800 border border-dark-700 text-dark-100 shadow-lg shadow-black/40 ${className}`}
              >
                <span className="relative z-10">{label}</span>
                {withArrow && coords && <span className={arrowClasses(coords.side)} />}
              </motion.div>
            )}
          </AnimatePresence>,
          document.body
        )}
    </>
  )
}
