import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { motion } from 'framer-motion'
import { ArrowDown, ArrowUp, ExternalLink, Globe, MapPin, Search, X } from 'lucide-react'
import { remnawaveApi, type RemnawaveGeoIpRow, type RemnawaveGeoIpsParams } from '../../api/client'
import { getFlag } from '../../utils/format'
import { CopyableIp } from '../ui/CopyableIp'
import { Toggle } from '../ui/Toggle'
import { Tooltip } from '../ui/Tooltip'
import { formatAgo } from './geo'
import { ProviderBadge } from './ProviderBadge'

const PAGE_SIZE = 50
const SEARCH_DEBOUNCE_MS = 300

type SortColumn = NonNullable<RemnawaveGeoIpsParams['sort']>

interface IpTableProps {
  bbox: string | null
  place: string | null
  node: string | null
  refreshKey: number
  onClearPlace: () => void
  onOpenUser: (userId: number) => void
}

export default function IpTable({ bbox, place, node, refreshKey, onClearPlace, onOpenUser }: IpTableProps) {
  const { t, i18n } = useTranslation()
  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')
  const [activeOnly, setActiveOnly] = useState(true)
  const [sort, setSort] = useState<SortColumn>('last_seen')
  const [dir, setDir] = useState<'asc' | 'desc'>('desc')
  const [rows, setRows] = useState<RemnawaveGeoIpRow[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const timer = window.setTimeout(() => setSearch(searchInput.trim()), SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [searchInput])

  const filterKey = JSON.stringify([bbox, place, node, search, activeOnly, sort, dir])
  const [page, setPage] = useState({ key: filterKey, offset: 0 })
  const offset = page.key === filterKey ? page.offset : 0
  const setOffset = (value: number) => setPage({ key: filterKey, offset: value })
  const requestId = useRef(0)

  const fetchRows = useCallback(async () => {
    const id = ++requestId.current
    try {
      const res = await remnawaveApi.getGeoIps({
        bbox: place ? undefined : bbox ?? undefined,
        place: place ?? undefined,
        node: node ?? undefined,
        search: search || undefined,
        active_only: activeOnly,
        sort,
        dir,
        offset,
        limit: PAGE_SIZE,
      })
      if (id !== requestId.current) return
      setRows(res.data.rows)
      setTotal(res.data.total)
    } catch { /* таблица останется с прошлыми данными до следующего обновления */ } finally {
      if (id === requestId.current) setLoading(false)
    }
  }, [bbox, place, node, search, activeOnly, sort, dir, offset])

  useEffect(() => { fetchRows() }, [fetchRows, refreshKey])

  const toggleSort = (column: SortColumn) => {
    if (sort === column) {
      setDir(d => (d === 'desc' ? 'asc' : 'desc'))
      return
    }
    setSort(column)
    setDir(column === 'last_seen' ? 'desc' : 'asc')
  }

  const lang = i18n.language
  const placeName = place?.split('|')[1]

  return (
    <div className="card !p-0 overflow-hidden">
      <div className="flex flex-wrap items-center gap-3 px-5 py-4 border-b border-dark-800/50">
        <div className="flex items-center gap-2 text-dark-200 text-sm font-medium">
          <Globe className="w-4 h-4" />
          {t('remnawave.geo.table_title')}
          <span className="text-dark-500 font-normal">{total.toLocaleString()}</span>
        </div>
        {bbox && !place && (
          <span className="text-xs text-dark-500">{t('remnawave.geo.table_visible_area')}</span>
        )}
        {place && (
          <button
            type="button"
            onClick={onClearPlace}
            className="inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded-lg bg-accent-500/15 text-accent-300 hover:bg-accent-500/25 transition-colors"
          >
            <MapPin className="w-3.5 h-3.5" />
            {getFlag(place.split('|')[0])} {placeName || '—'}
            <X className="w-3.5 h-3.5" />
          </button>
        )}
        <div className="flex-1" />
        <label className="flex items-center gap-2 text-xs text-dark-400 cursor-pointer select-none">
          <Toggle on={activeOnly} onClick={() => setActiveOnly(v => !v)} />
          {t('remnawave.geo.table_active_only')}
        </label>
        <div className="relative w-full sm:w-72">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-dark-500" />
          <input
            type="text"
            value={searchInput}
            onChange={e => setSearchInput(e.target.value)}
            placeholder={t('remnawave.geo.table_search')}
            className="input pl-9 !py-2 text-sm"
          />
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm min-w-[860px]">
          <thead>
            <tr className="border-b border-dark-800/50 text-dark-400 text-left">
              <SortHeader label={t('remnawave.geo.table_activity')} column="last_seen" sort={sort} dir={dir} onSort={toggleSort} />
              <SortHeader label={t('remnawave.geo.table_ip')} column="ip" sort={sort} dir={dir} onSort={toggleSort} />
              <th className="px-4 py-3 font-medium">{t('remnawave.geo.table_user')}</th>
              <th className="px-4 py-3 font-medium">{t('remnawave.geo.table_node')}</th>
              <SortHeader label={t('remnawave.geo.table_city')} column="city" sort={sort} dir={dir} onSort={toggleSort} />
              <SortHeader label={t('remnawave.geo.table_provider')} column="provider" sort={sort} dir={dir} onSort={toggleSort} />
            </tr>
          </thead>
          <tbody>
            {loading ? (
              Array.from({ length: 5 }).map((_, i) => (
                <tr key={i} className="border-b border-dark-800/30">
                  <td colSpan={6} className="px-4 py-3"><div className="h-5 bg-dark-700/50 rounded-lg animate-pulse" /></td>
                </tr>
              ))
            ) : rows.length === 0 ? (
              <tr><td colSpan={6} className="text-center py-10 text-dark-500">{t('remnawave.geo.table_empty')}</td></tr>
            ) : (
              rows.map(row => <IpRow key={row.ip} row={row} lang={lang} onOpenUser={onOpenUser} />)
            )}
          </tbody>
        </table>
      </div>

      {total > PAGE_SIZE && (
        <div className="flex items-center justify-center gap-4 py-3 border-t border-dark-800/50">
          <motion.button
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            disabled={offset === 0}
            className="btn btn-secondary text-sm"
            whileTap={{ scale: offset === 0 ? 1 : 0.98 }}
          >
            {t('common.prev', 'Prev')}
          </motion.button>
          <span className="text-dark-400 text-sm">
            {offset + 1}-{Math.min(offset + PAGE_SIZE, total)} / {total}
          </span>
          <motion.button
            onClick={() => setOffset(offset + PAGE_SIZE)}
            disabled={offset + PAGE_SIZE >= total}
            className="btn btn-secondary text-sm"
            whileTap={{ scale: offset + PAGE_SIZE >= total ? 1 : 0.98 }}
          >
            {t('common.next', 'Next')}
          </motion.button>
        </div>
      )}
    </div>
  )
}

function SortHeader({ label, column, sort, dir, onSort }: {
  label: string
  column: SortColumn
  sort: SortColumn
  dir: 'asc' | 'desc'
  onSort: (column: SortColumn) => void
}) {
  const active = sort === column
  return (
    <th
      onClick={() => onSort(column)}
      className={`px-4 py-3 font-medium cursor-pointer select-none transition-colors hover:text-dark-200 ${active ? 'text-accent-400' : ''}`}
    >
      <span className="inline-flex items-center gap-1">
        {label}
        {active && (dir === 'desc' ? <ArrowDown className="w-3.5 h-3.5" /> : <ArrowUp className="w-3.5 h-3.5" />)}
      </span>
    </th>
  )
}

function IpRow({ row, lang, onOpenUser }: { row: RemnawaveGeoIpRow; lang: string; onOpenUser: (id: number) => void }) {
  const { t } = useTranslation()
  const [firstNode, ...otherNodes] = row.nodes
  return (
    <tr className="border-b border-dark-800/30 hover:bg-dark-800/20 transition-colors">
      <td className="px-4 py-2.5 whitespace-nowrap">
        <span className="inline-flex items-center gap-2 text-dark-300">
          <span className={`w-2 h-2 rounded-full ${row.online ? 'bg-green-400 shadow-sm shadow-green-400/50' : 'bg-dark-600'}`} />
          {formatAgo(row.last_seen, lang)}
        </span>
      </td>
      <td className="px-4 py-2.5 whitespace-nowrap">
        <span className="inline-flex items-center gap-2">
          <CopyableIp value={row.ip} className="font-mono text-dark-100" />
          <Tooltip label={t('remnawave.geo.check_host')}>
            <a
              href={`https://check-host.net/ip-info?host=${encodeURIComponent(row.ip)}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-dark-500 hover:text-accent-400 transition-colors"
            >
              <ExternalLink className="w-3.5 h-3.5" />
            </a>
          </Tooltip>
        </span>
      </td>
      <td className="px-4 py-2.5">
        <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-0.5">
          {row.users.map(user => (
            <button
              key={user.id}
              type="button"
              onClick={() => onOpenUser(user.id)}
              className="text-dark-100 hover:text-accent-400 transition-colors truncate max-w-[160px]"
            >
              {user.username ?? `#${user.id}`}
            </button>
          ))}
          {row.users_total > row.users.length && (
            <span className="text-dark-500 text-xs">+{row.users_total - row.users.length}</span>
          )}
        </span>
      </td>
      <td className="px-4 py-2.5 whitespace-nowrap">
        {firstNode ? (
          <span className="inline-flex items-center gap-1.5 text-dark-200">
            <span>{getFlag(firstNode.country_code)}</span>
            <span className="truncate max-w-[140px]">{firstNode.name ?? firstNode.uuid.slice(0, 8)}</span>
            {otherNodes.length > 0 && (
              <Tooltip label={otherNodes.map(n => n.name ?? n.uuid.slice(0, 8)).join(', ')}>
                <span className="text-xs text-dark-500 cursor-default">+{otherNodes.length}</span>
              </Tooltip>
            )}
          </span>
        ) : <span className="text-dark-600">—</span>}
      </td>
      <td className="px-4 py-2.5 whitespace-nowrap">
        {row.relay ? (
          <span className="text-dark-400 italic">{t('remnawave.geo.via_relay', { name: row.relay })}</span>
        ) : row.place || row.country_code ? (
          <span className="inline-flex items-center gap-1.5 text-dark-200">
            <span>{getFlag(row.country_code)}</span>
            <span className="truncate max-w-[160px]">{row.place || row.country_code}</span>
          </span>
        ) : <span className="text-dark-600">—</span>}
      </td>
      <td className="px-4 py-2.5 max-w-[220px]">
        {row.relay ? <span className="text-dark-600">—</span> : <ProviderBadge asOrg={row.as_org} asn={row.asn} />}
      </td>
    </tr>
  )
}
