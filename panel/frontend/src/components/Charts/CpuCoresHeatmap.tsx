import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { useTranslation } from 'react-i18next'
import type { PerCpuHistory } from '../../api/client'
import { parseTimestamp } from '../../utils/chartUtils'
import { HEATMAP_SCALE, heatmapColor, formatDateLocalized, getDateTimeFormat } from './chartTheme'

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

const MIN_ROW_HEIGHT = 6
const MAX_ROW_HEIGHT = 14
const TARGET_HEIGHT = 160
// Подпись ядра не мельче этого; при более низких строках ядра подписываются через одно
const LABEL_MIN_HEIGHT = 12
// С такой высоты строки между ними остаётся зазор в 1px
const ROW_GAP_MIN_HEIGHT = 8
const CORE_LABEL_WIDTH = 36
const TIME_LABEL_COUNT = 6
const TOOLTIP_OFFSET = 14
const EMPTY_STATE_HEIGHT = 'h-24'

// Стабильные пустые значения: иначе эффект отрисовки перезапускался бы на каждом рендере без данных
const NO_CORES: (number | null)[][] = []
const NO_TIMESTAMPS: string[] = []

function rowHeightFor(cores: number): number {
  return Math.min(MAX_ROW_HEIGHT, Math.max(MIN_ROW_HEIGHT, TARGET_HEIGHT / cores))
}

function timeLabelColumns(cols: number): number[] {
  const count = Math.min(TIME_LABEL_COUNT, cols)
  if (count < 2) return cols > 0 ? [0] : []
  return Array.from({ length: count }, (_, k) => Math.round((k * (cols - 1)) / (count - 1)))
}

/**
 * Строка = ядро (ядро 0 сверху), столбец = бакет истории, цвет = загрузка 0–100 %.
 * Рисуется на canvas: 64 ядра × 288 бакетов как SVG-узлы тормозили бы страницу.
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
  const cellHeight = rowHeight >= ROW_GAP_MIN_HEIGHT ? rowHeight - 1 : rowHeight
  const labelStep = Math.max(1, Math.ceil(LABEL_MIN_HEIGHT / rowHeight))
  const canvasHeight = rows * rowHeight

  const times = useMemo(() => timestamps.map(parseTimestamp), [timestamps])
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

      const cellWidth = width / cols
      cores.forEach((values, row) => {
        values.forEach((value, col) => {
          if (value === null) return
          ctx.fillStyle = heatmapColor(value)
          const left = Math.floor(col * cellWidth)
          const right = Math.ceil((col + 1) * cellWidth)
          ctx.fillRect(left, row * rowHeight, right - left, cellHeight)
        })
      })
    }

    draw()
    const observer = new ResizeObserver(draw)
    observer.observe(wrap)
    return () => observer.disconnect()
  }, [cores, rows, cols, rowHeight, cellHeight, canvasHeight])

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

  return (
    <div>
      <div className="flex">
        <div className="relative shrink-0" style={{ width: CORE_LABEL_WIDTH, height: canvasHeight }}>
          {cores.map((_, core) => core % labelStep === 0 && (
            <span
              key={core}
              className="absolute right-2 text-[10px] leading-none font-mono text-dark-500"
              style={{ top: core * rowHeight + cellHeight / 2, transform: 'translateY(-50%)' }}
            >
              {core}
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
      </div>

      <div className="flex items-center justify-end gap-2 mt-2 text-[10px] text-dark-500">
        <span>{t('cpu_chart.heatmap_scale')}</span>
        <span className="font-mono">0%</span>
        <div
          className="h-2 w-24 rounded-sm"
          style={{ background: `linear-gradient(to right, ${HEATMAP_SCALE.join(', ')})` }}
        />
        <span className="font-mono">100%</span>
      </div>
    </div>
  )
}
