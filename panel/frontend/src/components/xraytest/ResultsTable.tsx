import { Fragment, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { CheckCircle2, ChevronDown, ChevronRight, XCircle, AlertTriangle, ShieldAlert } from 'lucide-react'
import type { XrayTestCell } from '../../api/client'
import { Checkbox } from '../ui/Checkbox'
import { Tooltip } from '../ui/Tooltip'

type SortKey = 'index' | 'rtt' | 'verdict'

const VERDICT_STYLE: Record<string, string> = {
  ok: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20',
  degraded: 'text-amber-400 bg-amber-500/10 border-amber-500/20',
  fail: 'text-red-400 bg-red-500/10 border-red-500/20',
}

function VerdictBadge({ verdict }: { verdict: string }) {
  const { t } = useTranslation()
  const Icon = verdict === 'ok' ? CheckCircle2 : verdict === 'degraded' ? AlertTriangle : XCircle
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md border text-xs font-medium ${VERDICT_STYLE[verdict] || VERDICT_STYLE.fail}`}>
      <Icon className="w-3.5 h-3.5" />
      {t(`xray_test.verdict_${verdict}`)}
    </span>
  )
}

function ms(value: number | null): string {
  if (value === null || value === undefined) return '—'
  return `${Math.round(value)}`
}

export function ResultsTable({ cells, groupBySni }: { cells: XrayTestCell[]; groupBySni: boolean }) {
  const { t } = useTranslation()
  const [sort, setSort] = useState<SortKey>('index')
  const [onlyWorking, setOnlyWorking] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const visible = useMemo(() => {
    const filtered = onlyWorking ? cells.filter(c => c.verdict !== 'fail') : cells
    const order = { ok: 0, degraded: 1, fail: 2 }
    return [...filtered].sort((a, b) => {
      if (sort === 'rtt') {
        const left = a.rtt_ms ?? Number.MAX_SAFE_INTEGER
        const right = b.rtt_ms ?? Number.MAX_SAFE_INTEGER
        return left - right
      }
      if (sort === 'verdict') return order[a.verdict] - order[b.verdict] || a.index - b.index
      return a.index - b.index
    })
  }, [cells, sort, onlyWorking])

  // Лучший SNI считается по всему набору, а не по видимому: фильтр не должен
  // менять, какой домен признан лучшим
  const bestBySni = useMemo(() => {
    if (!groupBySni) return new Set<number>()
    const best = new Map<string, XrayTestCell>()
    cells.forEach(cell => {
      if (cell.verdict === 'fail' || cell.rtt_ms === null) return
      const key = `${cell.address}:${cell.port}`
      const current = best.get(key)
      if (!current || (current.rtt_ms ?? Infinity) > cell.rtt_ms) best.set(key, cell)
    })
    return new Set([...best.values()].map(cell => cell.index))
  }, [cells, groupBySni])

  // Колонка места запуска нужна только когда прогон шёл больше чем из одной точки
  const showLocation = useMemo(
    () => new Set(cells.map(cell => cell.location)).size > 1,
    [cells],
  )

  const toggle = (key: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  if (!cells.length) {
    return (
      <div className="text-center py-10 text-dark-400 text-sm">
        {t('xray_test.no_results')}
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <label className="flex items-center gap-2 text-dark-300 cursor-pointer select-none">
          <Checkbox checked={onlyWorking} onChange={event => setOnlyWorking(event.target.checked)} />
          {t('xray_test.only_working')}
        </label>
        <div className="flex items-center gap-1 ml-auto">
          <span className="text-dark-500">{t('xray_test.sort_by')}</span>
          {(['index', 'rtt', 'verdict'] as SortKey[]).map(key => (
            <button
              key={key}
              onClick={() => setSort(key)}
              className={`px-2 py-1 rounded-md transition-colors ${
                sort === key ? 'bg-accent-500/15 text-accent-400' : 'text-dark-400 hover:text-dark-200'
              }`}
            >
              {t(`xray_test.sort_${key}`)}
            </button>
          ))}
        </div>
      </div>

      <div className="overflow-x-auto rounded-lg border border-dark-800/60">
        <table className="w-full text-xs">
          <thead className="bg-dark-900/70 sticky top-0 z-10">
            <tr className="text-dark-400 text-left">
              <th className="px-3 py-2 font-medium w-8" />
              <th className="px-3 py-2 font-medium">{t('xray_test.col_name')}</th>
              <th className="px-3 py-2 font-medium">{t('xray_test.col_address')}</th>
              <th className="px-3 py-2 font-medium">{t('xray_test.col_sni')}</th>
              {showLocation && (
                <th className="px-3 py-2 font-medium">{t('xray_test.col_location')}</th>
              )}
              <th className="px-3 py-2 font-medium">{t('xray_test.col_verdict')}</th>
              <th className="px-3 py-2 font-medium text-right">{t('xray_test.col_tcp')}</th>
              <th className="px-3 py-2 font-medium text-right">{t('xray_test.col_handshake')}</th>
              <th className="px-3 py-2 font-medium text-right">{t('xray_test.col_rtt')}</th>
              <th className="px-3 py-2 font-medium text-right">{t('xray_test.col_speed')}</th>
              <th className="px-3 py-2 font-medium">{t('xray_test.col_exit')}</th>
            </tr>
          </thead>
          <tbody>
            {visible.map(cell => {
              const key = `${cell.index}`
              const open = expanded.has(key)
              return (
                <Fragment key={key}>
                  <tr
                    onClick={() => toggle(key)}
                    className="border-t border-dark-800/40 hover:bg-dark-800/30 cursor-pointer"
                  >
                    <td className="px-3 py-2 text-dark-500">
                      {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
                    </td>
                    <td className="px-3 py-2 text-dark-200 max-w-[200px] truncate">
                      <span className="flex items-center gap-1.5">
                        {cell.remark || `#${cell.index + 1}`}
                        {bestBySni.has(cell.index) && (
                          <span className="px-1.5 py-0.5 rounded bg-accent-500/15 text-accent-400 text-[10px]">
                            {t('xray_test.best_sni')}
                          </span>
                        )}
                      </span>
                      <span className="text-dark-500 text-[10px]">
                        {cell.protocol} · {cell.transport} · {cell.security}
                        {cell.core ? ` · ${cell.core}` : ''}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-dark-300 font-mono text-[11px]">
                      {cell.address}:{cell.port}
                    </td>
                    <td className="px-3 py-2 text-dark-300 max-w-[160px] truncate">{cell.sni || '—'}</td>
                    {showLocation && (
                      <td className="px-3 py-2 text-dark-300 max-w-[140px] truncate">
                        {cell.location_name || t('xray_test.location_panel')}
                      </td>
                    )}
                    <td className="px-3 py-2">
                      {cell.reason ? (
                        <Tooltip label={t(`xray_test.reason_${cell.reason}`, cell.reason)}>
                          <span><VerdictBadge verdict={cell.verdict} /></span>
                        </Tooltip>
                      ) : (
                        <VerdictBadge verdict={cell.verdict} />
                      )}
                    </td>
                    <td className="px-3 py-2 text-right text-dark-300 tabular-nums">{ms(cell.tcp_min_ms)}</td>
                    <td className="px-3 py-2 text-right text-dark-300 tabular-nums">{ms(cell.handshake_ms)}</td>
                    <td className="px-3 py-2 text-right text-dark-200 tabular-nums">{ms(cell.rtt_ms)}</td>
                    <td className="px-3 py-2 text-right text-dark-300 tabular-nums">
                      {cell.speed_mbps ? cell.speed_mbps.toFixed(1) : '—'}
                    </td>
                    <td className="px-3 py-2 text-dark-300 font-mono text-[11px]">
                      {cell.exit_ip ? `${cell.exit_ip}${cell.exit_country ? ` (${cell.exit_country})` : ''}` : '—'}
                    </td>
                  </tr>
                  {open && (
                    <tr className="bg-dark-900/40">
                      <td />
                      <td colSpan={showLocation ? 10 : 9} className="px-3 py-3 space-y-2">
                        {cell.reason && (
                          <div className="text-dark-300">
                            <span className="text-dark-500">{t('xray_test.col_reason')}: </span>
                            {t(`xray_test.reason_${cell.reason}`, cell.reason)}
                          </div>
                        )}
                        {cell.detail && (
                          <pre className="text-[11px] font-mono text-dark-400 whitespace-pre-wrap break-all bg-dark-950/60 rounded p-2 max-h-32 overflow-auto">
                            {cell.detail}
                          </pre>
                        )}
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px] text-dark-400">
                          <Detail label={t('xray_test.detail_dns')} value={cell.resolved_ip} />
                          <Detail label={t('xray_test.detail_dns_ms')} value={ms(cell.dns_ms)} />
                          <Detail label={t('xray_test.detail_tcp_avg')} value={ms(cell.tcp_avg_ms)} />
                          <Detail label={t('xray_test.detail_jitter')} value={ms(cell.tcp_jitter_ms)} />
                          <Detail label={t('xray_test.detail_http')} value={cell.http_status} />
                          <Detail label={t('xray_test.detail_asn')} value={cell.exit_asn} />
                        </div>
                        {cell.tls && <TlsBlock tls={cell.tls} />}
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function Detail({ label, value }: { label: string; value: string | number | null | undefined }) {
  return (
    <div>
      <span className="text-dark-500">{label}: </span>
      <span className="text-dark-300">{value === null || value === undefined || value === '' ? '—' : value}</span>
    </div>
  )
}

function TlsBlock({ tls }: { tls: NonNullable<XrayTestCell['tls']> }) {
  const { t } = useTranslation()
  if (!tls.reachable) {
    return (
      <div className="flex items-center gap-2 text-[11px] text-amber-400">
        <ShieldAlert className="w-3.5 h-3.5" />
        {t('xray_test.tls_unreachable')}: {tls.error || '—'}
      </div>
    )
  }
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px] text-dark-400">
      <Detail label={t('xray_test.tls_issuer')} value={tls.issuer} />
      <Detail label={t('xray_test.tls_subject')} value={tls.subject} />
      <Detail label={t('xray_test.tls_version')} value={tls.version} />
      <Detail label={t('xray_test.tls_not_after')} value={tls.not_after} />
      {tls.self_signed && (
        <div className="col-span-2 md:col-span-4 flex items-center gap-2 text-amber-400">
          <ShieldAlert className="w-3.5 h-3.5" />
          {t('xray_test.tls_self_signed')}
        </div>
      )}
    </div>
  )
}
