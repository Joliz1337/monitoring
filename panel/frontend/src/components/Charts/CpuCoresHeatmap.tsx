import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { useTranslation } from 'react-i18next'
import type { PerCpuHistory } from '../../api/client'
import { parseTimestamp } from '../../utils/chartUtils'
import { HEATMAP_EMPTY_COLOR, heatmapColor, formatDateLocalized, getDateTimeFormat } from './chartTheme'

interface CpuCoresHeatmapProps {
  perCpu: PerCpuHistory | null
  period: string
  isLoading?: boolean
}

interface HoverCell {
  row: number
  col: number
  x: number
  y: number
}

// Высота строки по числу ядер: 8 ядер читаются как таблица, 64 — как полотно
const ROW_HEIGHT_BY_CORES: Array<[number, number]> = [[8, 26], [16, 20], [32, 14]]
const DENSE_ROW_HEIGHT = 9
const CELL_GAP = 2
const CELL_RADIUS = 3
// Ниже этой высоты строки подписи ядер идут через одно — иначе наезжают друг на друга
const LABEL_MIN_HEIGHT = 12
const CORE_LABEL_WIDTH = 56
const AVG_LABEL_WIDTH = 60
const TIME_LABEL_COUNT = 6
const TOOLTIP_OFFSET = 14
const EMPTY_STATE_HEIGHT = 'h-24'
const HOVER_OUTLINE = 'rgba(255, 255, 255, 0.6)'

// Образцы для легенды: середины диапазонов шкалы
const LEGEND_BINS = [
  { label: '0–25', sample: 12 },
  { label: '25–50', sample: 37 },
  { label: '50–80', sample: 65 },
  { label: '80–100', sample: 92 },
]

// Стабильные пустые значения: иначе эффект отрисовки перезапускался бы на каждом рендере без данных
const NO_CORES: (number | null)[][] = []
const NO_TIMESTAMPS: string[] = []

function rowHeightFor(cores: number): number {
  const match = ROW_HEIGHT_BY_CORES.find(([limit]) => cores <= limit)
  return match ? match[1] : DENSE_ROW_HEIGHT
}

function timeLabelColumns(cols: number): number[] {
  const count = Math.min(TIME_LABEL_COUNT, cols)
  if (count < 2) return cols > 0 ? [0] : []
  return Array.from({ length: count }, (_, k) => Math.round((k * (cols - 1)) / (count - 1)))
}

function coreAverage(values: (number | null)[]): number | null {
  let sum = 0
  let count = 0
  for (const value of values) {
    if (value === null) continue
    sum += value
    count += 1
  }
  return count > 0 ? sum / count : null
}

function fillCell(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number) {
  ctx.beginPath()
  ctx.roundRect(x, y, w, h, CELL_RADIUS)
  ctx.fill()
}

/**
 * Строка = ядро (ядро 0 сверху), ячейка = бакет истории, цвет = загрузка 0–100 %
 * по шкале зелёный → жёлтый → красный, как у живых баров по ядрам.
 * Рисуется на canvas: 64 ядра × 144 бакета как SVG-узлы тормозили бы страницу.
 */
export default function CpuCoresHeatmap({ perCpu, period, isLoading = false }: CpuCoresHeatmapProps) {
  const { t, i18n } = useTranslation()
  const wrapRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [hover, setHover] = useState<HoverCell | null>(null)

  const cores = perCpu?.cores ?? NO_CORES
  const timestamps = perCpu?.timestamps ?? NO_TIMESTAMPS
  const rows = cores.length
  const cols = timestamps.length
  const rowHeight = rowHeightFor(Math.max(rows, 1))
  const labelStep = Math.max(1, Math.ceil(LABEL_MIN_HEIGHT / rowHeight))
  const canvasHeight = rows * rowHeight

  const times = useMemo(() => timestamps.map(parseTimestamp), [timestamps])
  const averages = useMemo(() => cores.map(coreAverage), [cores])
  const labelColumns = useMemo(() => timeLabelColumns(cols), [cols])
  const dateFormat = getDateTimeFormat(period)
  const lang = i18n.language || 'en'

  useEffect(() => {
    const canvas = canvasRef.current
    const wrap = wrapRef.current
    if (!canvas || !wrap || rows === 0 || cols === 0) return

    const draw = () => {
      const width = wrap.clientWidth
      if (width === 0) return
      // Масштаб под devicePixelRatio, иначе на retina ячейки мылятся
      const dpr = window.devicePixelRatio || 1
      canvas.width = Math.round(width * dpr)
      canvas.height = Math.round(canvasHeight * dpr)
      canvas.style.width = `${width}px`
      canvas.style.height = `${canvasHeight}px`
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, width, canvasHeight)

      const slotWidth = width / cols
      const cellWidth = Math.max(1, slotWidth - CELL_GAP)
      const cellHeight = Math.max(1, rowHeight - CELL_GAP)
      cores.forEach((values, row) => {
        const top = row * rowHeight + CELL_GAP / 2
        values.forEach((value, col) => {
          ctx.fillStyle = value === null ? HEATMAP_EMPTY_COLOR : heatmapColor(value)
          fillCell(ctx, col * slotWidth + CELL_GAP / 2, top, cellWidth, cellHeight)
        })
      })

      if (hover) {
        ctx.strokeStyle = HOVER_OUTLINE
        ctx.lineWidth = 1
        ctx.beginPath()
        ctx.roundRect(hover.col * slotWidth + CELL_GAP / 2, hover.row * rowHeight + CELL_GAP / 2, cellWidth, cellHeight, CELL_RADIUS)
        ctx.stroke()
      }
    }

    draw()
    const observer = new ResizeObserver(draw)
    observer.observe(wrap)
    return () => observer.disconnect()
  }, [cores, rows, cols, rowHeight, canvasHeight, hover])

  const handleMouseMove = (event: MouseEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const x = event.clientX - rect.left
    const y = event.clientY - rect.top
    const col = Math.floor((x / rect.width) * cols)
    const row = Math.floor(y / rowHeight)
    if (col < 0 || col >= cols || row < 0 || row >= rows) {
      setHover(null)
      return
    }
    if (hover && hover.row === row && hover.col === col) return
    setHover({ row, col, x, y })
  }

  if (rows === 0 || cols === 0) {
    if (isLoading) return <div className={EMPTY_STATE_HEIGHT} />
    return (
      <div className={`flex items-center justify-center ${EMPTY_STATE_HEIGHT} text-sm text-dark-500`}>
        {t('cpu_chart.no_history')}
      </div>
    )
  }

  const hoverValue = hover ? cores[hover.row][hover.col] : null
  const wrapWidth = wrapRef.current?.clientWidth ?? 0
  const rowLabelStyle = (row: number) => ({ top: row * rowHeight + rowHeight / 2, transform: 'translateY(-50%)' })

  return (
    <div>
      <div className="flex">
        <div className="relative shrink-0" style={{ width: CORE_LABEL_WIDTH, height: canvasHeight }}>
          {cores.map((_, core) => core % labelStep === 0 && (
            <span
              key={core}
              className="absolute right-2.5 text-xs leading-none font-mono text-dark-500 whitespace-nowrap"
              style={rowLabelStyle(core)}
            >
              {t('cpu_chart.core')} {core}
            </span>
          ))}
        </div>

        <div ref={wrapRef} className="relative flex-1 min-w-0">
          <canvas
            ref={canvasRef}
            className="block"
            onMouseMove={handleMouseMove}
            onMouseLeave={() => setHover(null)}
          />
          {hover && (
            <div
              className="absolute z-10 pointer-events-none whitespace-nowrap rounded-md px-2.5 py-1.5 text-xs font-medium bg-dark-900 border border-dark-700 text-dark-100 shadow-lg shadow-black/40"
              style={{
                ...(hover.x < wrapWidth / 2 ? { left: hover.x + TOOLTIP_OFFSET } : { right: wrapWidth - hover.x + TOOLTIP_OFFSET }),
                ...(hover.y < canvasHeight / 2 ? { top: hover.y + TOOLTIP_OFFSET } : { bottom: canvasHeight - hover.y + TOOLTIP_OFFSET }),
              }}
            >
              {t('cpu_chart.core')} {hover.row}
              {' · '}
              {formatDateLocalized(new Date(times[hover.col]), dateFormat.tooltip, lang)}
              {' · '}
              {hoverValue === null ? t('common.no_data') : `${hoverValue.toFixed(0)}%`}
            </div>
          )}
        </div>

        <div className="relative shrink-0" style={{ width: AVG_LABEL_WIDTH, height: canvasHeight }}>
          {averages.map((average, core) => core % labelStep === 0 && (
            <span
              key={core}
              className="absolute left-2.5 text-xs leading-none font-mono text-dark-500 whitespace-nowrap"
              style={rowLabelStyle(core)}
            >
              {t('cpu_chart.avg')} <span className="text-dark-100">{average === null ? '—' : `${average.toFixed(0)}%`}</span>
            </span>
          ))}
        </div>
      </div>

      <div className="flex mt-1">
        <div className="shrink-0" style={{ width: CORE_LABEL_WIDTH }} />
        <div className="relative flex-1 h-4 text-[10px] font-mono text-dark-500">
          {labelColumns.map((col, index) => (
            <span
              key={col}
              className="absolute top-0"
              style={{
                left: `${((col + 0.5) / cols) * 100}%`,
                transform: index === 0 ? 'none' : index === labelColumns.length - 1 ? 'translateX(-100%)' : 'translateX(-50%)',
              }}
            >
              {formatDateLocalized(new Date(times[col]), dateFormat.xaxis, lang)}
            </span>
          ))}
        </div>
        <div className="shrink-0" style={{ width: AVG_LABEL_WIDTH }} />
      </div>

      <div className="flex items-center gap-4 mt-3 text-xs text-dark-500">
        {LEGEND_BINS.map(bin => (
          <span key={bin.label} className="flex items-center gap-1.5">
            <span className="w-3 h-3 rounded-[3px]" style={{ background: heatmapColor(bin.sample) }} />
            {bin.label}%
          </span>
        ))}
        <span className="ml-auto flex items-center gap-1.5">
          <span className="w-3 h-3 rounded-[3px]" style={{ background: HEATMAP_EMPTY_COLOR }} />
          {t('cpu_chart.empty_cell')}
        </span>
      </div>
    </div>
  )
}
