import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { motion } from 'framer-motion'
import { toast } from 'sonner'
import { History, Loader2, Map as MapIcon, MapPin, Play, Server, Smartphone, Users, Wifi } from 'lucide-react'
import {
  remnawaveApi,
  type RemnawaveApiNode, type RemnawaveCollectorStatus, type RemnawaveGeoMap, type RemnawaveSummary,
} from '../../api/client'
import { useAutoRefresh } from '../../hooks/useAutoRefresh'
import { getFlag } from '../../utils/format'
import { Tooltip } from '../ui/Tooltip'
import GeoMap from './GeoMap'
import IpTable from './IpTable'
import UserGeoModal from './UserGeoModal'

const REFRESH_INTERVAL_MS = 60000

export default function OverviewTab() {
  const { t } = useTranslation()
  const [summary, setSummary] = useState<RemnawaveSummary | null>(null)
  const [status, setStatus] = useState<RemnawaveCollectorStatus | null>(null)
  const [nodes, setNodes] = useState<RemnawaveApiNode[]>([])
  const [geo, setGeo] = useState<RemnawaveGeoMap | null>(null)
  const [loading, setLoading] = useState(true)
  const [collecting, setCollecting] = useState(false)
  const [node, setNode] = useState<string | null>(null)
  const [place, setPlace] = useState<string | null>(null)
  const [bbox, setBbox] = useState<string | null>(null)
  const [openUser, setOpenUser] = useState<number | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)

  const fetchData = useCallback(async () => {
    const [summaryRes, statusRes, nodesRes, geoRes] = await Promise.allSettled([
      remnawaveApi.getSummary(),
      remnawaveApi.getCollectorStatus(),
      remnawaveApi.getNodes(),
      remnawaveApi.getGeoMap(node ?? undefined),
    ])
    // Упавший запрос оставляет свой блок с прошлыми данными до следующего обновления
    if (summaryRes.status === 'fulfilled') setSummary(summaryRes.value.data)
    if (statusRes.status === 'fulfilled') setStatus(statusRes.value.data)
    if (nodesRes.status === 'fulfilled') setNodes(nodesRes.value.data.nodes || [])
    if (geoRes.status === 'fulfilled') setGeo(geoRes.value.data)
    setRefreshKey(k => k + 1)
    setLoading(false)
  }, [node])

  useEffect(() => { fetchData() }, [fetchData])
  useAutoRefresh(fetchData, { customInterval: REFRESH_INTERVAL_MS })

  const handleCollect = async () => {
    setCollecting(true)
    try {
      const res = await remnawaveApi.collectNow()
      if (res.data.success) {
        toast.success(t('remnawave.collectSuccess'))
        await fetchData()
      } else {
        toast.error(res.data.error || 'Error')
      }
    } catch { toast.error('Error') } finally { setCollecting(false) }
  }

  const selectNode = (uuid: string) => {
    setNode(current => (current === uuid ? null : uuid))
    setPlace(null)
  }

  const closeUser = useCallback(() => setOpenUser(null), [])

  const ipsByNode = new Map((geo?.nodes ?? []).map(n => [n.uuid, n.ips]))
  const connectedNodes = nodes.filter(n => n.is_connected && !n.is_disabled)
  const totalOnline = connectedNodes.reduce((sum, n) => sum + (n.users_online || 0), 0)
  const historyDays = summary?.history_days ?? 30

  const tiles: { icon: ReactNode; value: number | undefined; label: string; tone: string }[] = [
    { icon: <Users className="w-4 h-4" />, value: summary?.unique_users, label: t('remnawave.geo.kpi_users_online'), tone: 'text-accent-400 bg-accent-500/10' },
    { icon: <Wifi className="w-4 h-4" />, value: summary?.unique_ips, label: t('remnawave.geo.kpi_ips_online'), tone: 'text-emerald-400 bg-emerald-500/10' },
    { icon: <MapPin className="w-4 h-4" />, value: summary?.cities_online, label: t('remnawave.geo.kpi_cities'), tone: 'text-amber-400 bg-amber-500/10' },
    { icon: <History className="w-4 h-4" />, value: summary?.ips_history, label: t('remnawave.geo.kpi_ips_history', { days: historyDays }), tone: 'text-violet-400 bg-violet-500/10' },
    { icon: <Smartphone className="w-4 h-4" />, value: summary?.total_devices, label: t('remnawave.geo.kpi_devices'), tone: 'text-sky-400 bg-sky-500/10' },
  ]

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -12 }}
      className="space-y-4"
    >
      <div className="flex items-center justify-end gap-3">
        {status && (
          <div className="flex items-center gap-2 text-sm text-dark-400">
            <div className={`w-2 h-2 rounded-full ${status.running ? 'bg-green-400 shadow-sm shadow-green-400/50' : 'bg-dark-600'}`} />
            {status.running ? t('remnawave.running') : t('remnawave.stopped')}
            {status.next_collect_in != null && <span className="text-dark-500">({status.next_collect_in}s)</span>}
          </div>
        )}
        <motion.button
          onClick={handleCollect}
          disabled={collecting}
          className="btn btn-primary text-sm"
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
        >
          {collecting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
          {t('remnawave.collectNow')}
        </motion.button>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        {tiles.map(tile => (
          <div key={tile.label} className="card !p-3 flex items-center gap-3">
            <div className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 ${tile.tone}`}>{tile.icon}</div>
            <div className="min-w-0">
              {loading ? (
                <div className="h-6 w-14 bg-dark-700/50 rounded animate-pulse" />
              ) : (
                <div className="text-lg font-bold text-dark-50 leading-tight">{(tile.value ?? 0).toLocaleString()}</div>
              )}
              <div className="text-xs text-dark-400 truncate">{tile.label}</div>
            </div>
          </div>
        ))}
      </div>

      {nodes.length > 0 && (
        <div className="card !p-3 space-y-2">
          <div className="flex items-center justify-between gap-2 text-xs text-dark-400 px-1">
            <span className="flex items-center gap-1.5 text-dark-200 font-medium text-sm">
              <Server className="w-4 h-4" />
              {t('remnawave.nodes')}
            </span>
            <span>
              {connectedNodes.length}/{nodes.length} {t('remnawave.nodesOnline')}, {totalOnline} {t('remnawave.nodesUsers')}
            </span>
          </div>
          <div className="flex flex-wrap gap-2">
            {nodes.map(n => {
              const selected = node === n.uuid
              const statusDot = n.is_disabled ? 'bg-dark-600' : n.is_connected ? 'bg-green-400' : 'bg-red-400'
              return (
                <Tooltip key={n.uuid} label={t('remnawave.geo.nodes_hint')} openDelay={600}>
                  <button
                    type="button"
                    onClick={() => selectNode(n.uuid)}
                    className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-sm transition-colors ${
                      selected
                        ? 'bg-accent-500/15 border-accent-500/40 text-accent-200'
                        : 'bg-dark-800/40 border-dark-800/40 text-dark-200 hover:bg-dark-800/70'
                    }`}
                  >
                    <span>{getFlag(n.country_code)}</span>
                    <span className="truncate max-w-[140px]">{n.name}</span>
                    <span className="text-xs text-dark-500">{ipsByNode.get(n.uuid) ?? 0}</span>
                    <span className={`w-1.5 h-1.5 rounded-full ${statusDot}`} />
                  </button>
                </Tooltip>
              )
            })}
          </div>
        </div>
      )}

      <div className="space-y-2">
        <div className="flex items-center gap-2 text-dark-200 text-sm font-medium px-1">
          <MapIcon className="w-4 h-4" />
          {t('remnawave.geo.map_title')}
        </div>
        <GeoMap
          data={geo}
          node={node}
          selectedPlace={place}
          onSelectPlace={setPlace}
          onBoundsChange={setBbox}
          onOpenUser={setOpenUser}
        />
      </div>

      <IpTable
        bbox={bbox}
        place={place}
        node={node}
        refreshKey={refreshKey}
        onClearPlace={() => setPlace(null)}
        onOpenUser={setOpenUser}
      />

      {openUser !== null && <UserGeoModal userId={openUser} onClose={closeUser} />}
    </motion.div>
  )
}
