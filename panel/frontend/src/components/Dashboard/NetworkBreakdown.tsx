import { useEffect, useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { trafficApi, type TrafficBreakdown, type TrafficBreakdownRow } from '../../api/client'
import { formatBitsPerSecLocalized } from '../../utils/format'

// Окно открыто, пока на него смотрят — обновляем его чаще, чем раз в цикл дашборда
const REFRESH_MS = 10_000
const SPARK_WIDTH = 90
const SPARK_HEIGHT = 22
const SPARK_PAD = 2

interface NetworkBreakdownProps {
  serverId: number
}

export default function NetworkBreakdown({ serverId }: NetworkBreakdownProps) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { uid } = useParams()
  const [data, setData] = useState<TrafficBreakdown | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const { data: breakdown } = await trafficApi.getBreakdown(serverId)
        if (cancelled) return
        setData(breakdown)
        setFailed(false)
      } catch {
        if (!cancelled) setFailed(true)
      }
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [serverId])

  const openTraffic = () => navigate(`/${uid}/server/${serverId}/traffic`)

  return (
    <div
      className="w-[480px] max-w-[calc(100vw-16px)] p-3 cursor-pointer"
      onClick={openTraffic}
    >
      {!data ? (
        <p className="py-6 text-center text-xs text-dark-500">
          {failed ? t('network_breakdown.failed') : t('common.loading')}
        </p>
      ) : (
        <>
          <Section title={t('network_breakdown.by_ip')}>
            {!data.ips_supported ? (
              <Hint>{t('network_breakdown.update_node', { version: data.min_ip_version })}</Hint>
            ) : data.ips.length === 0 ? (
              <Hint>{t('network_breakdown.no_ips')}</Hint>
            ) : (
              data.ips.map(row => <BreakdownRow key={row.key} row={row} label={row.key} />)
            )}
          </Section>

          <Section title={t('network_breakdown.by_interface')}>
            {data.interfaces.length === 0 ? (
              <Hint>{t('network_breakdown.no_data')}</Hint>
            ) : (
              data.interfaces.map(row => <BreakdownRow key={row.key} row={row} label={row.key} />)
            )}
          </Section>

          <Section title={t('network_breakdown.by_port')}>
            {data.ports.length === 0 ? (
              <Hint>{t('network_breakdown.no_ports')}</Hint>
            ) : (
              data.ports.map(row => <BreakdownRow key={row.key} row={row} label={`:${row.key}`} />)
            )}
          </Section>

          <p className="mt-2 text-[11px] text-dark-500">{t('network_breakdown.footer')}</p>
        </>
      )}
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="mb-2 last:mb-0">
      <div className="mb-1 text-[11px] uppercase tracking-wide text-dark-500">{title}</div>
      <div className="divide-y divide-dark-700/50">{children}</div>
    </div>
  )
}

function Hint({ children }: { children: ReactNode }) {
  return <p className="py-1 text-xs text-dark-500">{children}</p>
}

function BreakdownRow({ row, label }: { row: TrafficBreakdownRow; label: string }) {
  const { t } = useTranslation()
  const speed = (value: number | null) => (value === null ? '—' : formatBitsPerSecLocalized(value, t))

  // Колонка подписи вмещает IPv4 целиком; пометка падения встаёт под адрес, а не рядом,
  // чтобы не обрезать его до «185.…»
  return (
    <div className="flex items-center gap-2 py-1 font-mono text-xs">
      <div className="flex w-[120px] flex-shrink-0 flex-col items-start gap-0.5">
        <span className="max-w-full truncate text-dark-200">{label}</span>
        {row.drop && (
          <span className="rounded-md bg-warning/15 px-1.5 py-px font-sans text-[10px] text-warning">
            {row.drop.ratio === null
              ? t('network_breakdown.gone')
              : t('network_breakdown.dropped', { count: Math.round(row.drop.ratio) })}
          </span>
        )}
      </div>
      <Sparkline points={row.sparkline} className={row.drop ? 'text-warning' : 'text-accent-400'} />
      <span className="ml-auto w-[104px] whitespace-nowrap text-right text-success">↓ {speed(row.rx_bytes_per_sec)}</span>
      <span className="w-[104px] whitespace-nowrap text-right text-accent-400">↑ {speed(row.tx_bytes_per_sec)}</span>
    </div>
  )
}

// Масштаб у каждой строки свой: важна форма — шёл трафик и упал, — а величину
// показывают цифры рядом. Часы без наблюдений рвут линию, а не тянут её к нулю.
function Sparkline({ points, className }: { points: Array<number | null>; className: string }) {
  const max = Math.max(0, ...points.map(value => value ?? 0))
  const step = points.length > 1 ? SPARK_WIDTH / (points.length - 1) : 0
  const usable = SPARK_HEIGHT - SPARK_PAD * 2

  const segments: Array<Array<[number, number]>> = []
  let current: Array<[number, number]> = []
  points.forEach((value, index) => {
    if (value === null) {
      if (current.length) segments.push(current)
      current = []
      return
    }
    const y = SPARK_HEIGHT - SPARK_PAD - (max > 0 ? (value / max) * usable : 0)
    current.push([index * step, y])
  })
  if (current.length) segments.push(current)

  return (
    <svg width={SPARK_WIDTH} height={SPARK_HEIGHT} className={`flex-shrink-0 ${className}`} aria-hidden="true">
      {segments.map((segment, index) =>
        segment.length === 1 ? (
          <circle key={index} cx={segment[0][0]} cy={segment[0][1]} r={1.5} fill="currentColor" />
        ) : (
          <polyline
            key={index}
            points={segment.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ')}
            fill="none"
            stroke="currentColor"
            strokeWidth={1.5}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        )
      )}
    </svg>
  )
}
