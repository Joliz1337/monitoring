import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'
import { motion } from 'framer-motion'
import { CheckCircle2, Loader2, RotateCcw, Route, X, XCircle } from 'lucide-react'
import {
  remnawaveApi, type ClientRoute, type ClientRouteHop, type ClientRouteLeg, type ClientRouteStop,
  type RemnawaveNodeBrief,
} from '../../api/client'
import { getFlag } from '../../utils/format'
import { Tooltip } from '../ui/Tooltip'
import { ROUTE_LEG_COLORS, ROUTE_STOP_COLORS } from './geo'
import { ProviderBadge } from './ProviderBadge'
import RouteMap from './RouteMap'

const POLL_INTERVAL_MS = 1_000
const POLL_ATTEMPTS = 3
const ROUTE_ERRORS = new Set(['node_not_in_panel', 'node_location_unknown'])

interface RouteModalProps {
  ip: string
  nodes: RemnawaveNodeBrief[]
  countryCode: string | null
  place: string | null
  asOrg: string | null
  asn: number | null
  onClose: () => void
}

interface RouteRun {
  serverId: number
  serverName: string
  traceId: string
  nodeUuid: string
}

function errorCode(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
  return typeof detail === 'string' ? detail : 'unreachable'
}

export default function RouteModal({ ip, nodes, countryCode, place, asOrg, asn, onClose }: RouteModalProps) {
  const { t } = useTranslation()
  const [nodeUuid, setNodeUuid] = useState(nodes[0]?.uuid ?? '')
  const [run, setRun] = useState<RouteRun | null>(null)
  const [route, setRoute] = useState<ClientRoute | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [hoveredLeg, setHoveredLeg] = useState<number | null>(null)
  const [focus, setFocus] = useState<{ leg: number; nonce: number } | null>(null)

  // Быстрое переключение ноды: ответ запуска, пришедший позже следующего, отбрасывается
  const startSeq = useRef(0)
  const start = useCallback(async () => {
    const seq = ++startSeq.current
    setRun(null)
    setRoute(null)
    setError(null)
    setFocus(null)
    try {
      const { data } = await remnawaveApi.startClientRoute(ip, nodeUuid)
      if (seq !== startSeq.current) return
      setRun({ serverId: data.server_id, serverName: data.server_name, traceId: data.trace_id, nodeUuid })
    } catch (err) {
      if (seq === startSeq.current) setError(errorCode(err))
    }
  }, [ip, nodeUuid])

  useEffect(() => { start() }, [start])

  // Опрос раз в секунду, пока трасса идёт: города появляются по ходу. Разовый сбой связи
  // (панель перезапускается, нода не ответила) не обрывает окно — трасса на ноде идёт дальше
  useEffect(() => {
    if (!run) return
    let stopped = false
    let failures = 0
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const { data } = await remnawaveApi.getClientRoute(run.serverId, run.traceId, run.nodeUuid)
        if (stopped) return
        failures = 0
        setRoute(data)
        if (data.state === 'running') timer = setTimeout(poll, POLL_INTERVAL_MS)
      } catch (err) {
        if (stopped) return
        const code = errorCode(err)
        if (code === 'unreachable' && ++failures < POLL_ATTEMPTS) timer = setTimeout(poll, POLL_INTERVAL_MS)
        else setError(code)
      }
    }
    poll()
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [run])

  // Escape закрывает только это окно: перехват на погружении, до карточки пользователя под ним
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.stopPropagation()
      onClose()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [onClose])

  const node = nodes.find(n => n.uuid === nodeUuid)
  const nodeName = node?.name ?? run?.serverName ?? nodeUuid.slice(0, 8)
  const running = !error && (!route || route.state === 'running')
  const errorText = error
    ? (ROUTE_ERRORS.has(error)
      ? t(`remnawave.geo.route_error_${error}`)
      : t(`loss.trace_error_${error}`, { defaultValue: t('loss.trace_error_unreachable') }))
    : null

  // Портал: окно открывается и из карточки пользователя, а её анимация (transform) сдвигала бы
  // position: fixed; клики гасятся здесь, чтобы не сворачивать строку пользователя под окном
  return createPortal(
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4"
      onClick={e => { e.stopPropagation(); onClose() }}
    >
      <motion.div
        className="card !bg-dark-950 !p-0 w-full max-w-6xl h-[min(88vh,780px)] flex flex-col overflow-hidden"
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        onClick={e => e.stopPropagation()}
      >
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3 border-b border-dark-800/60">
          <h2 className="text-base font-semibold text-dark-100 flex items-center gap-2">
            <Route className="w-5 h-5 text-accent-500" />
            {t('remnawave.geo.route_title')}
          </h2>
          <span className="font-mono text-sm text-dark-200">{ip}</span>
          {(place || countryCode) && (
            <span className="text-sm text-dark-300">{getFlag(countryCode)} {place || countryCode}</span>
          )}
          <span className="max-w-[220px]"><ProviderBadge asOrg={asOrg} asn={asn} /></span>
          <div className="flex-1" />
          {nodes.length > 1 && (
            <div className="flex flex-wrap items-center gap-1.5 text-xs text-dark-400">
              {t('remnawave.geo.route_node')}
              {nodes.map(n => (
                <button
                  key={n.uuid}
                  type="button"
                  onClick={() => setNodeUuid(n.uuid)}
                  className={`px-2 py-1 rounded-lg border transition-colors ${n.uuid === nodeUuid
                    ? 'border-accent-500/60 text-accent-300 bg-accent-500/10'
                    : 'border-dark-700/60 text-dark-300 hover:text-dark-100'}`}
                >
                  {getFlag(n.country_code)} {n.name ?? n.uuid.slice(0, 8)}
                </button>
              ))}
            </div>
          )}
          <button
            type="button"
            onClick={start}
            disabled={running}
            className="btn btn-secondary text-xs !py-1.5 flex items-center gap-1.5 disabled:opacity-50"
          >
            <RotateCcw className="w-3.5 h-3.5" />
            {t('remnawave.geo.route_repeat')}
          </button>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-200" aria-label={t('common.close')}>
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 min-h-0 grid grid-cols-1 grid-rows-[minmax(280px,1fr)_auto] lg:grid-rows-1 lg:grid-cols-[minmax(0,1fr)_360px]">
          <div className="min-h-0 border-b lg:border-b-0 lg:border-r border-dark-800/60">
            <RouteMap route={route} nodeName={nodeName} focus={focus} hoveredLeg={hoveredLeg} onHoverLeg={setHoveredLeg} />
          </div>
          <div className="min-h-0 overflow-y-auto p-4 space-y-4">
            <RouteStatus route={route} running={running} errorText={errorText} />
            {route && <RouteTotals route={route} />}
            {route && route.stops.length > 0 && (
              <RouteSteps
                route={route}
                nodeName={nodeName}
                hoveredLeg={hoveredLeg}
                onHoverLeg={setHoveredLeg}
                onFocusLeg={leg => setFocus(prev => ({ leg, nonce: (prev?.nonce ?? 0) + 1 }))}
              />
            )}
            {route && !route.client_located && (
              <p className="text-xs text-dark-400">{t('remnawave.geo.route_client_unknown')}</p>
            )}
            <RouteLegend />
            <p className="text-[11px] leading-relaxed text-dark-500">{t('remnawave.geo.route_hint')}</p>
          </div>
        </div>
      </motion.div>
    </div>,
    document.body,
  )
}

function RouteStatus({ route, running, errorText }: { route: ClientRoute | null; running: boolean; errorText: string | null }) {
  const { t } = useTranslation()
  if (errorText) {
    return <StatusLine tone="text-danger" icon={<XCircle className="w-4 h-4" />} text={errorText} />
  }
  if (route?.state === 'failed') {
    return <StatusLine tone="text-danger" icon={<XCircle className="w-4 h-4" />} text={t('remnawave.geo.route_failed', { error: route.error ?? '' })} />
  }
  if (running) {
    const text = route
      ? t('remnawave.geo.route_running', { done: route.rounds_done, total: route.rounds_total })
      : t('remnawave.geo.route_starting')
    return <StatusLine tone="text-dark-300" icon={<Loader2 className="w-4 h-4 animate-spin" />} text={text} />
  }
  return <StatusLine tone="text-success" icon={<CheckCircle2 className="w-4 h-4" />} text={t('remnawave.geo.route_done')} />
}

function StatusLine({ tone, icon, text }: { tone: string; icon: ReactNode; text: string }) {
  return (
    <div className={`flex items-start gap-2 text-sm ${tone}`}>
      <span className="mt-0.5 shrink-0">{icon}</span>
      {text}
    </div>
  )
}

function RouteTotals({ route }: { route: ClientRoute }) {
  const { t } = useTranslation()
  const km = (value: number) => t('remnawave.geo.route_km_value', { value: Math.round(value).toLocaleString() })
  const tiles = [
    {
      label: t('remnawave.geo.route_rtt'),
      value: route.totals.rtt_ms !== null ? t('remnawave.geo.route_ms', { value: Math.round(route.totals.rtt_ms) }) : '—',
      hint: route.client_reached ? null : t('remnawave.geo.route_rtt_partial'),
    },
    { label: t('remnawave.geo.route_km'), value: km(route.totals.km), hint: null },
    { label: t('remnawave.geo.route_direct_km'), value: km(route.totals.direct_km), hint: null },
  ]
  return (
    <div className="grid grid-cols-3 gap-2">
      {tiles.map(tile => (
        <div key={tile.label} className="rounded-xl bg-dark-800/40 border border-dark-700/40 px-3 py-2">
          <div className="text-[11px] text-dark-400">{tile.label}</div>
          <div className="text-base font-semibold text-dark-100">{tile.value}</div>
          {tile.hint && <div className="text-[10px] leading-tight text-dark-500">{tile.hint}</div>}
        </div>
      ))}
    </div>
  )
}

function RouteSteps({ route, nodeName, hoveredLeg, onHoverLeg, onFocusLeg }: {
  route: ClientRoute
  nodeName: string
  hoveredLeg: number | null
  onHoverLeg: (index: number | null) => void
  onFocusLeg: (index: number) => void
}) {
  return (
    <div className="space-y-0.5">
      {route.stops.map((stop, index) => (
        <Fragment key={`${index}-${stop.lon}-${stop.lat}`}>
          <StopRow stop={stop} nodeName={nodeName} />
          {route.legs[index] && (
            <LegRow
              leg={route.legs[index]}
              active={hoveredLeg === index}
              onHover={hover => onHoverLeg(hover ? index : null)}
              onFocus={() => onFocusLeg(index)}
            />
          )}
        </Fragment>
      ))}
      {route.hidden_tail.length > 0 && <HiddenHops hops={route.hidden_tail} tail />}
    </div>
  )
}

function StopRow({ stop, nodeName }: { stop: ClientRouteStop; nodeName: string }) {
  const { t } = useTranslation()
  const asNames = [...new Set(stop.hops.map(h => h.as_name).filter(Boolean))].slice(0, 2).join(', ')
  const title = stop.kind === 'node' ? nodeName : stop.place ?? stop.country_code ?? '—'
  const size = stop.kind === 'router' ? 10 : 12
  return (
    <div className="flex items-center gap-2.5 py-1">
      <span className="w-4 flex justify-center shrink-0">
        <span className="block rounded-full" style={{ width: size, height: size, border: `2px solid ${ROUTE_STOP_COLORS[stop.kind]}` }} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-sm text-dark-100 truncate">
          {getFlag(stop.country_code)} {title}
          {stop.kind === 'client' && <span className="text-dark-500 text-xs"> · {t('remnawave.geo.route_kind_client')}</span>}
        </div>
        {asNames && <div className="text-[11px] text-dark-500 truncate">{asNames}</div>}
      </div>
      {stop.kind !== 'node' && stop.rtt_ms !== null && (
        <span className="text-xs text-dark-300 tabular-nums">{t('remnawave.geo.route_ms', { value: Math.round(stop.rtt_ms) })}</span>
      )}
    </div>
  )
}

function LegRow({ leg, active, onHover, onFocus }: {
  leg: ClientRouteLeg
  active: boolean
  onHover: (hover: boolean) => void
  onFocus: () => void
}) {
  const { t } = useTranslation()
  const color = ROUTE_LEG_COLORS[leg.status ?? 'unknown']
  const way = leg.last_mile
    ? t('remnawave.geo.route_leg_last_mile')
    : t(leg.mode === 'rail' ? 'remnawave.geo.route_leg_rail' : 'remnawave.geo.route_leg_direct')
  return (
    <div
      className={`flex gap-2.5 rounded-lg cursor-pointer transition-colors ${active ? 'bg-dark-800/60' : 'hover:bg-dark-800/40'}`}
      onMouseEnter={() => onHover(true)}
      onMouseLeave={() => onHover(false)}
      onClick={onFocus}
    >
      <span className="w-4 flex justify-center shrink-0">
        <span
          className="w-[3px] my-0.5 rounded-full"
          style={leg.last_mile
            ? { backgroundImage: `linear-gradient(${color} 50%, transparent 50%)`, backgroundSize: '3px 6px' }
            : { background: color, boxShadow: `0 0 6px ${color}` }}
        />
      </span>
      <div className="py-1.5 text-xs space-y-0.5 min-w-0">
        <div className="text-dark-300">
          {leg.added_ms !== null && (
            <b className="font-semibold" style={{ color }}>+{t('remnawave.geo.route_ms', { value: Math.round(leg.added_ms) })} </b>
          )}
          {t('remnawave.geo.route_km_value', { value: Math.round(leg.km).toLocaleString() })} · {way}
        </div>
        {leg.status && leg.status !== 'ok' && (
          <div style={{ color }}>
            {t(`remnawave.geo.route_status_${leg.status}`)}, {t('remnawave.geo.route_expected', { value: Math.max(1, Math.round(leg.expected_ms)) })}
          </div>
        )}
        {leg.hidden.length > 0 && <HiddenHops hops={leg.hidden} />}
      </div>
    </div>
  )
}

function HiddenHops({ hops, tail = false }: { hops: ClientRouteHop[]; tail?: boolean }) {
  const { t } = useTranslation()
  const details = hops
    .map(hop => `#${hop.hop} ${hop.host ?? '???'} — ${t(`remnawave.geo.route_hidden_${hop.reason ?? 'silent'}`)}`)
    .join('\n')
  return (
    <Tooltip label={<span className="whitespace-pre-line">{details}</span>}>
      <span className={`text-[11px] text-dark-500 underline decoration-dotted underline-offset-2 cursor-help ${tail ? 'ml-6' : ''}`}>
        {t(tail ? 'remnawave.geo.route_hidden_tail' : 'remnawave.geo.route_hidden', { count: hops.length })}
      </span>
    </Tooltip>
  )
}

function RouteLegend() {
  const { t } = useTranslation()
  const items: { color: string; label: string; dashed?: boolean }[] = [
    { color: ROUTE_LEG_COLORS.ok, label: t('remnawave.geo.route_legend_ok') },
    { color: ROUTE_LEG_COLORS.warn, label: t('remnawave.geo.route_legend_warn') },
    { color: ROUTE_LEG_COLORS.bad, label: t('remnawave.geo.route_legend_bad') },
    { color: ROUTE_LEG_COLORS.unknown, label: t('remnawave.geo.route_legend_unknown'), dashed: true },
  ]
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-dark-400">
      {items.map(item => (
        <span key={item.label} className="inline-flex items-center gap-1.5">
          <span
            className="w-4 h-[3px] rounded-full"
            style={item.dashed
              ? { backgroundImage: `linear-gradient(90deg, ${item.color} 50%, transparent 50%)`, backgroundSize: '6px 3px' }
              : { background: item.color }}
          />
          {item.label}
        </span>
      ))}
    </div>
  )
}
