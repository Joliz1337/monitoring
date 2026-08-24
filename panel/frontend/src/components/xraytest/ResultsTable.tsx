import { Fragment, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  CheckCircle2, ChevronDown, ChevronRight, XCircle, AlertTriangle,
  ShieldAlert, X, Lightbulb, Server, MapPin, Globe,
} from 'lucide-react'
import type { XrayTestCell } from '../../api/client'
import { Tooltip } from '../ui/Tooltip'

type SortKey = 'index' | 'rtt' | 'verdict'
type Verdict = 'ok' | 'degraded' | 'fail'

const VERDICT_STYLE: Record<string, string> = {
  ok: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20',
  degraded: 'text-amber-400 bg-amber-500/10 border-amber-500/20',
  fail: 'text-red-400 bg-red-500/10 border-red-500/20',
}

const VERDICT_ORDER: Record<string, number> = { ok: 0, degraded: 1, fail: 2 }

/** Узел дерева результатов: сервер, внутри — места запуска, внутри — SNI. */
interface Group {
  key: string
  label: string
  cells: XrayTestCell[]
  children: Group[]
}

function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${Math.round(value)}`
}

function best(cells: XrayTestCell[], pick: (cell: XrayTestCell) => number | null): number | null {
  const values = cells
    .map(pick)
    .filter((value): value is number => value !== null && value !== undefined)
  return values.length ? Math.min(...values) : null
}

function summarize(cells: XrayTestCell[]) {
  const counts: Record<Verdict, number> = { ok: 0, degraded: 0, fail: 0 }
  cells.forEach(cell => { counts[cell.verdict as Verdict] += 1 })
  // Итог группы — лучшее, что в ней есть: один рабочий SNI делает сервер пригодным
  const verdict: Verdict = counts.ok ? 'ok' : counts.degraded ? 'degraded' : 'fail'
  return { counts, verdict, working: counts.ok + counts.degraded, total: cells.length }
}

export function ResultsTable({ cells, groupBySni }: { cells: XrayTestCell[]; groupBySni: boolean }) {
  const { t } = useTranslation()
  const [sort, setSort] = useState<SortKey>('index')
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set())
  const [openCells, setOpenCells] = useState<Set<number>>(new Set())

  const counts = useMemo(() => {
    const totals: Record<string, number> = { ok: 0, degraded: 0, fail: 0 }
    cells.forEach(cell => { totals[cell.verdict] = (totals[cell.verdict] || 0) + 1 })
    return totals
  }, [cells])

  const visible = useMemo(
    // Пустой набор означает «показывать всё»: фильтр не может спрятать таблицу целиком
    () => (picked.size ? cells.filter(cell => picked.has(cell.verdict)) : cells),
    [cells, picked],
  )

  /**
   * Сервер → места запуска → проверки. Промежуточный уровень появляется только
   * когда он что-то различает: с одной локацией лишняя вложенность заставляла
   * бы раскрывать группу ради единственного списка.
   */
  const groups = useMemo<Group[]>(() => {
    const byServer = new Map<string, XrayTestCell[]>()
    visible.forEach(cell => {
      const key = `${cell.address}:${cell.port}`
      if (!byServer.has(key)) byServer.set(key, [])
      byServer.get(key)!.push(cell)
    })

    const result: Group[] = [...byServer.entries()].map(([key, serverCells]) => {
      const locations = new Set(serverCells.map(cell => cell.location))
      let children: Group[] = []

      if (locations.size > 1) {
        const byLocation = new Map<string, XrayTestCell[]>()
        serverCells.forEach(cell => {
          if (!byLocation.has(cell.location)) byLocation.set(cell.location, [])
          byLocation.get(cell.location)!.push(cell)
        })
        children = [...byLocation.entries()].map(([location, locationCells]) => ({
          key: `${key}|${location}`,
          label: locationCells[0].location_name || t('xray_test.location_panel'),
          cells: locationCells,
          children: [],
        }))
      }

      return { key, label: serverCells[0].remark || key, cells: serverCells, children }
    })

    const weight = (group: Group) => {
      if (sort === 'verdict') {
        return VERDICT_ORDER[summarize(group.cells).verdict] * 1e9 + group.cells[0].index
      }
      if (sort === 'rtt') return best(group.cells, cell => cell.rtt_ms) ?? Number.MAX_SAFE_INTEGER
      return group.cells[0].index
    }
    return result.sort((left, right) => weight(left) - weight(right))
  }, [visible, sort, t])

  const toggleVerdict = (verdict: string) => {
    setPicked(prev => {
      const next = new Set(prev)
      if (next.has(verdict)) next.delete(verdict)
      else next.add(verdict)
      return next
    })
  }

  const toggleGroup = (key: string) => {
    setOpenGroups(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const toggleCell = (index: number) => {
    setOpenCells(prev => {
      const next = new Set(prev)
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return next
    })
  }

  const sortCells = (list: XrayTestCell[]) => [...list].sort((a, b) => {
    if (sort === 'rtt') {
      return (a.rtt_ms ?? Number.MAX_SAFE_INTEGER) - (b.rtt_ms ?? Number.MAX_SAFE_INTEGER)
    }
    if (sort === 'verdict') {
      return VERDICT_ORDER[a.verdict] - VERDICT_ORDER[b.verdict] || a.index - b.index
    }
    return a.index - b.index
  })

  if (!cells.length) {
    return <div className="text-center py-10 text-dark-400 text-sm">{t('xray_test.no_results')}</div>
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <div className="flex flex-wrap items-center gap-2">
          {(['ok', 'degraded', 'fail'] as const).map(verdict => (
            <FilterChip
              key={verdict}
              verdict={verdict}
              count={counts[verdict] || 0}
              active={picked.has(verdict)}
              onClick={() => toggleVerdict(verdict)}
            />
          ))}
          {picked.size > 0 && (
            <button
              className="flex items-center gap-1 px-2 py-1 rounded-md text-dark-400 hover:text-dark-200"
              onClick={() => setPicked(new Set())}
            >
              <X className="w-3 h-3" />
              {t('xray_test.reset_filter')}
            </button>
          )}
        </div>

        <span className="text-dark-500">
          {t('xray_test.servers_count', { count: groups.length })}
        </span>

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

      {groups.length === 0 ? (
        <p className="text-center py-8 text-sm text-dark-400">{t('xray_test.filter_empty')}</p>
      ) : (
        <div className="space-y-2">
          {groups.map(group => (
            <ServerCard
              key={group.key}
              group={group}
              open={openGroups.has(group.key)}
              openGroups={openGroups}
              openCells={openCells}
              onToggleGroup={toggleGroup}
              onToggleCell={toggleCell}
              sortCells={sortCells}
              groupBySni={groupBySni}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function ServerCard({
  group, open, openGroups, openCells, onToggleGroup, onToggleCell, sortCells, groupBySni,
}: {
  group: Group
  open: boolean
  openGroups: Set<string>
  openCells: Set<number>
  onToggleGroup: (key: string) => void
  onToggleCell: (index: number) => void
  sortCells: (cells: XrayTestCell[]) => XrayTestCell[]
  groupBySni: boolean
}) {
  const { t } = useTranslation()
  const summary = summarize(group.cells)
  const sample = group.cells[0]
  const locationCount = new Set(group.cells.map(cell => cell.location)).size
  const remarks = new Set(group.cells.map(cell => cell.remark).filter(Boolean))

  return (
    <div className="rounded-lg border border-dark-800/60 overflow-hidden">
      <div
        className="flex items-center gap-3 px-3 py-2.5 cursor-pointer hover:bg-dark-800/30 transition-colors"
        onClick={() => onToggleGroup(group.key)}
      >
        <span className="text-dark-500 shrink-0">
          {open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
        </span>
        <Server className="w-4 h-4 text-dark-500 shrink-0" />

        <span className="flex-1 min-w-0">
          <span className="block text-sm text-dark-200 truncate">
            {remarks.size > 1
              ? t('xray_test.several_configs', { count: remarks.size })
              : group.label}
          </span>
          <span className="block text-[11px] text-dark-500 font-mono truncate">
            {sample.address}:{sample.port} · {sample.protocol} · {sample.transport} · {sample.security}
            {sample.core ? ` · ${sample.core}` : ''}
          </span>
        </span>

        <span className="hidden md:flex items-center gap-4 text-[11px] shrink-0">
          <Metric label={t('xray_test.col_tcp')} value={ms(best(group.cells, c => c.tcp_min_ms))} />
          <Metric label={t('xray_test.best_rtt')} value={ms(best(group.cells, c => c.rtt_ms))} />
        </span>

        <span className="text-[11px] text-dark-400 shrink-0 tabular-nums">
          {summary.working}/{summary.total}
        </span>
        {locationCount > 1 && (
          <span className="hidden sm:inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-dark-800/70 text-dark-400 text-[10px] shrink-0">
            <MapPin className="w-2.5 h-2.5" />
            {locationCount}
          </span>
        )}
        <VerdictBadge verdict={summary.verdict} />
      </div>

      {open && (
        <div className="border-t border-dark-800/60 bg-dark-900/30">
          {group.children.length > 0
            ? group.children.map(child => (
                <LocationBlock
                  key={child.key}
                  group={child}
                  open={openGroups.has(child.key)}
                  openCells={openCells}
                  onToggleGroup={onToggleGroup}
                  onToggleCell={onToggleCell}
                  sortCells={sortCells}
                  groupBySni={groupBySni}
                />
              ))
            : (
              <CheckList
                cells={sortCells(group.cells)}
                openCells={openCells}
                onToggleCell={onToggleCell}
                groupBySni={groupBySni}
              />
            )}
        </div>
      )}
    </div>
  )
}

function LocationBlock({
  group, open, openCells, onToggleGroup, onToggleCell, sortCells, groupBySni,
}: {
  group: Group
  open: boolean
  openCells: Set<number>
  onToggleGroup: (key: string) => void
  onToggleCell: (index: number) => void
  sortCells: (cells: XrayTestCell[]) => XrayTestCell[]
  groupBySni: boolean
}) {
  const { t } = useTranslation()
  const summary = summarize(group.cells)

  return (
    <div className="border-b border-dark-800/40 last:border-b-0">
      <div
        className="flex items-center gap-3 px-3 py-2 pl-8 cursor-pointer hover:bg-dark-800/30 transition-colors"
        onClick={() => onToggleGroup(group.key)}
      >
        <span className="text-dark-500 shrink-0">
          {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        </span>
        <MapPin className="w-3.5 h-3.5 text-dark-500 shrink-0" />
        <span className="flex-1 min-w-0 text-xs text-dark-200 truncate">{group.label}</span>

        <span className="hidden md:flex items-center gap-4 text-[11px] shrink-0">
          <Metric label={t('xray_test.col_tcp')} value={ms(best(group.cells, c => c.tcp_min_ms))} />
          <Metric label={t('xray_test.best_rtt')} value={ms(best(group.cells, c => c.rtt_ms))} />
        </span>

        <span className="text-[11px] text-dark-400 shrink-0 tabular-nums">
          {summary.working}/{summary.total}
        </span>
        <VerdictBadge verdict={summary.verdict} small />
      </div>

      {open && (
        <div className="pl-4">
          <CheckList
            cells={sortCells(group.cells)}
            openCells={openCells}
            onToggleCell={onToggleCell}
            groupBySni={groupBySni}
          />
        </div>
      )}
    </div>
  )
}

function CheckList({ cells, openCells, onToggleCell, groupBySni }: {
  cells: XrayTestCell[]
  openCells: Set<number>
  onToggleCell: (index: number) => void
  groupBySni: boolean
}) {
  const { t } = useTranslation()

  // Внутри блока локации она одна и подпись не нужна. Но если уровень локаций
  // не выделялся, соседние строки с одинаковым SNI различает только место
  // запуска — без подписи они выглядят одинаковыми
  const showLocation = useMemo(
    () => new Set(cells.map(cell => cell.location)).size > 1,
    [cells],
  )

  // Один и тот же адрес попадается в разных профилях подписки: проверки идут
  // отдельные, а адрес и SNI у них совпадают — различает только имя
  const showRemark = useMemo(
    () => new Set(cells.map(cell => cell.remark)).size > 1,
    [cells],
  )

  // Лучший SNI считается по всей группе: отметка не должна прыгать от сортировки
  const bestIndex = useMemo(() => {
    if (!groupBySni) return null
    const alive = cells.filter(cell => cell.verdict !== 'fail' && cell.rtt_ms !== null)
    if (!alive.length) return null
    return alive.reduce((a, b) => ((a.rtt_ms ?? Infinity) <= (b.rtt_ms ?? Infinity) ? a : b)).index
  }, [cells, groupBySni])

  return (
    <div className="divide-y divide-dark-800/40">
      {cells.map(cell => {
        const open = openCells.has(cell.index)
        return (
          <Fragment key={cell.index}>
            <div
              className="flex items-center gap-3 px-3 py-2 pl-8 cursor-pointer hover:bg-dark-800/20 transition-colors"
              onClick={() => onToggleCell(cell.index)}
            >
              <span className="text-dark-600 shrink-0">
                {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
              </span>
              <Globe className="w-3.5 h-3.5 text-dark-600 shrink-0" />

              <span className="flex-1 min-w-0 flex items-center gap-1.5">
                <span className="text-xs text-dark-300 truncate">
                  {cell.sni || t('xray_test.sni_from_key')}
                </span>
                {showRemark && cell.remark && (
                  <span className="px-1.5 py-0.5 rounded bg-dark-800/70 text-dark-300 text-[10px] shrink-0 max-w-[220px] truncate">
                    {cell.remark}
                  </span>
                )}
                {showLocation && (
                  <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-dark-800/70 text-dark-300 text-[10px] shrink-0">
                    <MapPin className="w-2.5 h-2.5" />
                    {cell.location_name || t('xray_test.location_panel')}
                  </span>
                )}
                {bestIndex === cell.index && (
                  <span className="px-1.5 py-0.5 rounded bg-accent-500/15 text-accent-400 text-[10px] shrink-0">
                    {t('xray_test.best_sni')}
                  </span>
                )}
              </span>

              <span className="hidden lg:flex items-center gap-4 text-[11px] shrink-0 tabular-nums">
                <Metric label={t('xray_test.col_tcp')} value={ms(cell.tcp_min_ms)} />
                <Metric label={t('xray_test.col_handshake')} value={ms(cell.handshake_ms)} />
                <Metric label={t('xray_test.col_rtt')} value={ms(cell.rtt_ms)} />
                {cell.speed_mbps ? (
                  <Metric label={t('xray_test.col_speed')} value={cell.speed_mbps.toFixed(1)} />
                ) : null}
              </span>

              <span className="hidden xl:block text-[11px] text-dark-400 font-mono shrink-0 w-40 truncate">
                {cell.exit_ip
                  ? `${cell.exit_ip}${cell.exit_country ? ` (${cell.exit_country})` : ''}`
                  : '—'}
              </span>

              {cell.reason ? (
                <Tooltip label={t(`xray_test.reason_${cell.reason}`, cell.reason)}>
                  <span><VerdictBadge verdict={cell.verdict} small /></span>
                </Tooltip>
              ) : (
                <VerdictBadge verdict={cell.verdict} small />
              )}
            </div>

            {open && <CellDetails cell={cell} />}
          </Fragment>
        )
      })}
    </div>
  )
}

function CellDetails({ cell }: { cell: XrayTestCell }) {
  const { t } = useTranslation()
  return (
    <div className="px-3 py-3 pl-14 space-y-2 bg-dark-950/40 text-xs">
      {cell.reason && (
        <div className="text-dark-300">
          <span className="text-dark-500">{t('xray_test.col_reason')}: </span>
          {t(`xray_test.reason_${cell.reason}`, cell.reason)}
        </div>
      )}
      {cell.hint && (
        <div className="flex items-start gap-2 px-2.5 py-2 rounded-md bg-amber-500/10 border border-amber-500/20 text-amber-300">
          <Lightbulb className="w-3.5 h-3.5 mt-0.5 shrink-0" />
          <span>{t(`xray_test.hint_${cell.hint}`, '')}</span>
        </div>
      )}
      {cell.detail && (
        <div>
          <span className="text-dark-500 text-[11px]">{t('xray_test.core_says')}</span>
          <pre className="mt-1 text-[11px] font-mono text-dark-300 whitespace-pre-wrap break-all bg-dark-950/60 rounded p-2 max-h-32 overflow-auto">
            {cell.detail}
          </pre>
        </div>
      )}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px] text-dark-400">
        <Detail label={t('xray_test.detail_dns')} value={cell.resolved_ip} />
        <Detail label={t('xray_test.detail_dns_ms')} value={ms(cell.dns_ms)} />
        <Detail label={t('xray_test.detail_tcp_avg')} value={ms(cell.tcp_avg_ms)} />
        <Detail label={t('xray_test.detail_jitter')} value={ms(cell.tcp_jitter_ms)} />
        <Detail label={t('xray_test.detail_http')} value={cell.http_status} />
        <Detail label={t('xray_test.detail_asn')} value={cell.exit_asn} />
        <Detail label={t('xray_test.col_exit')} value={cell.exit_ip} />
        <Detail
          label={t('xray_test.col_location')}
          value={cell.location_name || t('xray_test.location_panel')}
        />
      </div>
      {cell.tls && <TlsBlock tls={cell.tls} />}
    </div>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <span className="whitespace-nowrap">
      <span className="text-dark-600">{label} </span>
      <span className="text-dark-300">{value}</span>
    </span>
  )
}

function VerdictBadge({ verdict, small }: { verdict: string; small?: boolean }) {
  const { t } = useTranslation()
  const Icon = verdict === 'ok' ? CheckCircle2 : verdict === 'degraded' ? AlertTriangle : XCircle
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-md border font-medium shrink-0 ${
        small ? 'px-1.5 py-0.5 text-[10px]' : 'px-2 py-0.5 text-xs'
      } ${VERDICT_STYLE[verdict] || VERDICT_STYLE.fail}`}
    >
      <Icon className={small ? 'w-3 h-3' : 'w-3.5 h-3.5'} />
      {t(`xray_test.verdict_${verdict}`)}
    </span>
  )
}

const CHIP_STYLE: Record<string, { active: string; idle: string }> = {
  ok: {
    active: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40',
    idle: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20 hover:border-emerald-500/40',
  },
  degraded: {
    active: 'bg-amber-500/20 text-amber-300 border-amber-500/40',
    idle: 'bg-amber-500/10 text-amber-400 border-amber-500/20 hover:border-amber-500/40',
  },
  fail: {
    active: 'bg-red-500/20 text-red-300 border-red-500/40',
    idle: 'bg-red-500/10 text-red-400 border-red-500/20 hover:border-red-500/40',
  },
}

function FilterChip({ verdict, count, active, onClick }: {
  verdict: string
  count: number
  active: boolean
  onClick: () => void
}) {
  const { t } = useTranslation()
  const style = CHIP_STYLE[verdict] || CHIP_STYLE.fail
  const Icon = verdict === 'ok' ? CheckCircle2 : verdict === 'degraded' ? AlertTriangle : XCircle

  return (
    <button
      onClick={onClick}
      disabled={count === 0 && !active}
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border transition-all ${
        active ? style.active : style.idle
      } ${count === 0 && !active ? 'opacity-40 cursor-not-allowed' : ''}`}
    >
      <Icon className="w-3.5 h-3.5" />
      {t(`xray_test.verdict_${verdict}`)}
      <span className="font-semibold tabular-nums">{count}</span>
    </button>
  )
}

function Detail({ label, value }: { label: string; value: string | number | null | undefined }) {
  return (
    <div>
      <span className="text-dark-500">{label}: </span>
      <span className="text-dark-300">
        {value === null || value === undefined || value === '' ? '—' : value}
      </span>
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
