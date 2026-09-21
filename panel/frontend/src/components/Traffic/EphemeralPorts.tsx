import { useState } from 'react'
import { ChevronRight, Hash } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import ProgressBar from '../ui/ProgressBar'
import { FAQIcon } from '../FAQ'
import type { EphemeralPorts as EphemeralPortsData } from '../../api/client'

interface EphemeralPortsProps {
  data: EphemeralPortsData
}

// Пороги те же, что у полосы заполнения, иначе зелёное «свободно» спорило бы
// с красной полосой на одном и том же адресе
const freeColor = (held: number, capacity: number): string => {
  const percent = capacity > 0 ? (held / capacity) * 100 : 0
  if (percent >= 85) return 'text-danger'
  if (percent >= 60) return 'text-warning'
  return 'text-success'
}

export default function EphemeralPorts({ data }: EphemeralPortsProps) {
  const { t } = useTranslation()
  const [expanded, setExpanded] = useState<string | null>(data.sources[0]?.ip ?? null)

  if (data.sources.length === 0) return null

  const format = (value: number) => value.toLocaleString()

  return (
    <div className="card">
      <h3 className="font-semibold text-dark-100 mb-1 flex items-center gap-2">
        <Hash className="w-4 h-4 text-accent-500" />
        {t('ephemeral.title')}
        <FAQIcon screen="TRAFFIC_EPHEMERAL_PORTS" size="sm" />
      </h3>
      <p className="text-xs text-dark-400 mb-4">
        {t('ephemeral.range', {
          low: data.range_low,
          high: data.range_high,
          capacity: format(data.capacity),
        })}
      </p>

      <div className="space-y-2">
        {data.sources.map(source => {
          const isOpen = expanded === source.ip
          // Потолок считается на пару «наш адрес → адрес:порт цели», поэтому
          // близость к нему показывает ближайшее к потолку направление, а не
          // сумма адреса: с тремя направлениями она втрое больше потолка.
          // Занятое и остаток считает нода — там учтён tcp_tw_reuse
          const tightest = source.destinations[0]
          return (
            <div key={source.ip} className="bg-dark-800/50 rounded-lg overflow-hidden">
              <button
                type="button"
                onClick={() => setExpanded(isOpen ? null : source.ip)}
                className="w-full p-3 text-left hover:bg-dark-800/80 transition-colors"
              >
                <div className="flex items-center gap-2 mb-2">
                  <ChevronRight
                    className={`w-4 h-4 text-dark-500 transition-transform ${isOpen ? 'rotate-90' : ''}`}
                  />
                  <span className="font-mono text-dark-100 truncate">{source.ip}</span>
                  <span className="ml-auto text-xs text-dark-400 whitespace-nowrap">
                    {t('ephemeral.destinations_count', { count: source.destinations_total })}
                  </span>
                </div>
                <ProgressBar value={tightest?.held ?? 0} max={data.capacity} size="sm" />
                <div className="flex items-center justify-between mt-1.5 text-xs">
                  <span className="text-dark-400">
                    {t('ephemeral.peak_usage', { used: format(tightest?.held ?? 0) })}
                  </span>
                  <span className={`font-mono ${freeColor(tightest?.held ?? 0, data.capacity)}`}>
                    {t('ephemeral.free', { free: format(tightest?.free ?? data.capacity) })}
                  </span>
                </div>
              </button>

              {isOpen && (
                <div className="px-3 pb-3 space-y-2 border-t border-dark-700/50 pt-3">
                  {source.destinations.map(destination => (
                    <div key={`${destination.ip}:${destination.port}`}>
                      <div className="flex items-center justify-between text-xs mb-1 gap-2">
                        <span className="font-mono text-dark-300 truncate">
                          {destination.ip}:{destination.port}
                        </span>
                        <span className="font-mono text-dark-400 whitespace-nowrap">
                          {format(destination.held)} / {format(data.capacity)}
                        </span>
                      </div>
                      <ProgressBar value={destination.held} max={data.capacity} size="sm" />
                    </div>
                  ))}
                  {source.destinations_total > source.destinations.length && (
                    <p className="text-xs text-dark-500 pt-1">
                      {t('ephemeral.more_destinations', {
                        count: source.destinations_total - source.destinations.length,
                      })}
                    </p>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
