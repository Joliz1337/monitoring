import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CardScale, DetailLevel } from '../../stores/settingsStore'
import ServerCard, { GRID_CARD_HEIGHT_ESTIMATE, areServerCardPropsEqual, type ServerCardProps } from './ServerCard'

// Карточка монтируется, когда её слот ближе этого запаса к экрану: при обычной
// прокрутке она готова раньше, чем доедет до видимой области
const NEAR_VIEWPORT_MARGIN = '800px 0px'

// Строка компактного списка всегда одной высоты: ручка 32px, поля и рамка карточки
const COMPACT_CARD_HEIGHT = 74

type NearListener = (isNear: boolean) => void

const nearListeners = new WeakMap<Element, NearListener>()
let nearObserver: IntersectionObserver | null = null

// Один observer на все слоты. Новая подписка всегда начинается с уведомления о текущем
// положении слота — на этом держится повторная подписка в LazyCardSlot
function observeNear(slot: Element, listener: NearListener) {
  nearObserver ??= new IntersectionObserver(
    entries => entries.forEach(entry => nearListeners.get(entry.target)?.(entry.isIntersecting)),
    { rootMargin: NEAR_VIEWPORT_MARGIN },
  )
  const observer = nearObserver
  nearListeners.set(slot, listener)
  observer.observe(slot)
  return () => {
    nearListeners.delete(slot)
    observer.unobserve(slot)
  }
}

interface SlotHeight {
  /** Вид карточек, в котором сделан замер: в другом виде высота уже не годится */
  layoutKey: string
  height: number
}

export interface CardPlaceholder extends SlotHeight {
  /** Есть, пока высота — прикидка: карточки, смонтированные первыми, заменяют её замером */
  onMeasure?: (height: number) => void
}

function median(values: number[]) {
  const sorted = [...values].sort((a, b) => a - b)
  return sorted[Math.floor(sorted.length / 2)]
}

/** Высота заглушек для текущего вида карточек: сначала прикидка, затем замер первых карточек */
export function useCardPlaceholder(compact: boolean, detailLevel: DetailLevel, cardScale: CardScale): CardPlaceholder {
  const layoutKey = compact ? 'compact' : `${detailLevel}:${cardScale}`
  const fallbackHeight = compact ? COMPACT_CARD_HEIGHT : GRID_CARD_HEIGHT_ESTIMATE
  const [samples, setSamples] = useState<{ layoutKey: string; heights: number[] } | null>(null)

  return useMemo(() => {
    const heights = samples?.layoutKey === layoutKey ? samples.heights : null
    return {
      layoutKey,
      // Медиана, а не первая карточка: ряд с сервером на десятки ядер или ряд, растянутый
      // соседней заглушкой, дал бы высоту, далёкую от остальных
      height: heights ? median(heights) : fallbackHeight,
      onMeasure: heights
        ? undefined
        : (height: number) => setSamples(prev => ({
            layoutKey,
            heights: prev?.layoutKey === layoutKey ? [...prev.heights, height] : [height],
          })),
    }
  }, [layoutKey, samples, fallbackHeight])
}

interface LazyServerCardProps extends Omit<ServerCardProps, 'index' | 'lazy'> {
  placeholder: CardPlaceholder
  /** Идёт drag: смонтированные карточки остаются в DOM, dnd-kit держит их замеры */
  holdMounted: boolean
  /** Эту карточку сейчас перетаскивают */
  dragged: boolean
}

function LazyCardSlot({ placeholder, holdMounted, dragged, ...cardProps }: LazyServerCardProps) {
  const { layoutKey, onMeasure } = placeholder
  const slotRef = useRef<HTMLDivElement>(null)
  // Перенос в другую папку посреди drag создаёт карточке новый слот. Она монтируется
  // сразу, в том же коммите, как и без ленивого режима: иначе вместо неё пару кадров
  // пустая заглушка, а dnd-kit на это время остаётся без узла активной карточки
  const [mounted, setMounted] = useState(dragged)
  const [leftHeight, setLeftHeight] = useState<SlotHeight | null>(null)

  // Подписка пересоздаётся со сменой holdMounted, и observer заново сообщает положение
  // слота: всё, что уехало от экрана за время drag, размонтируется уже по раскладке
  // после дропа, а брошенная карточка не моргает
  useEffect(() => {
    const slot = slotRef.current
    if (!slot) return
    return observeNear(slot, isNear => {
      if (isNear) {
        setMounted(true)
        return
      }
      if (holdMounted) return
      // Высоту запоминаем, пока карточка ещё в DOM: заглушка встанет точно на её место,
      // и страница под пользователем не сдвинется
      if (slot.firstElementChild) setLeftHeight({ layoutKey, height: slot.getBoundingClientRect().height })
      setMounted(false)
    })
  }, [holdMounted, layoutKey])

  useLayoutEffect(() => {
    if (mounted && onMeasure && slotRef.current) onMeasure(slotRef.current.getBoundingClientRect().height)
  }, [mounted, onMeasure])

  const ownHeight = leftHeight?.layoutKey === layoutKey ? leftHeight.height : null

  // Сетка растягивает карточку на высоту ряда, как раньше растягивалась она сама:
  // dnd-kit меряет карточку, и от её размера зависят сдвиги соседей при перетаскивании
  return (
    <div
      ref={slotRef}
      className="grid grid-cols-1"
      style={mounted ? undefined : { height: ownHeight ?? placeholder.height }}
    >
      {mounted && <ServerCard {...cardProps} lazy />}
    </div>
  )
}

/**
 * Карточка сервера для больших флотов: настоящая — только рядом с экраном,
 * в остальное время её место держит пустая заглушка той же высоты
 */
const LazyServerCard = memo(LazyCardSlot, (prev, next) =>
  prev.placeholder === next.placeholder
  && prev.holdMounted === next.holdMounted
  && prev.dragged === next.dragged
  && areServerCardPropsEqual(prev, next))

export default LazyServerCard
