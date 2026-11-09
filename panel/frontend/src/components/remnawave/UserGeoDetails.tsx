import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { motion } from 'framer-motion'
import { ExternalLink, Smartphone, Trash2 } from 'lucide-react'
import {
  remnawaveApi, type RemnawaveNodeBrief, type RemnawaveUserDetails, type RemnawaveUserHistoryIp,
} from '../../api/client'
import { formatBytes, getFlag } from '../../utils/format'
import { CopyableIp } from '../ui/CopyableIp'
import { Tooltip } from '../ui/Tooltip'
import ClientMiniMap from './ClientMiniMap'
import { formatAgo } from './geo'
import { ProviderBadge } from './ProviderBadge'
import RouteButton from './RouteButton'
import RouteModal from './RouteModal'

interface UserGeoDetailsProps {
  details: RemnawaveUserDetails
  onDeleteIp?: (ip: string) => void
  onDeleteAllIps?: () => void
}

export default function UserGeoDetails({ details, onDeleteIp, onDeleteAllIps }: UserGeoDetailsProps) {
  const { t, i18n } = useTranslation()
  // undefined — ещё грузится, null — Remnawave не отдал расход
  const [trafficBytes, setTrafficBytes] = useState<number | null | undefined>(undefined)

  useEffect(() => {
    let alive = true
    setTrafficBytes(undefined)
    remnawaveApi.getUserTraffic(details.email)
      .then(res => { if (alive) setTrafficBytes(res.data.bytes) })
      .catch(() => { if (alive) setTrafficBytes(null) })
    return () => { alive = false }
  }, [details.email])

  const [routeIp, setRouteIp] = useState<{ ip: RemnawaveUserHistoryIp; nodes: RemnawaveNodeBrief[] } | null>(null)
  const nodesByUuid = useMemo(() => new Map(details.nodes.map(n => [n.uuid, n])), [details.nodes])
  const lang = i18n.language
  const days = details.history_days

  const chips: { label: string; value: string }[] = [
    {
      label: t('remnawave.geo.user_traffic_today'),
      value: trafficBytes === undefined ? '…' : trafficBytes === null ? '—' : formatBytes(trafficBytes),
    },
    { label: t('remnawave.geo.user_cities'), value: String(details.summary.cities) },
    { label: t('remnawave.geo.user_providers'), value: String(details.summary.providers) },
    { label: t('remnawave.geo.user_nodes'), value: String(details.summary.nodes) },
    { label: t('remnawave.geo.user_ips_history', { days }), value: String(details.summary.ips_history) },
  ]

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {chips.map(chip => (
          <span key={chip.label} className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-dark-800/60 border border-dark-700/40 text-xs text-dark-400">
            {chip.label} <b className="text-dark-100 font-semibold">{chip.value}</b>
          </span>
        ))}
        <div className="flex-1" />
        {onDeleteAllIps && details.history.length > 0 && (
          <motion.button
            onClick={e => { e.stopPropagation(); onDeleteAllIps() }}
            className="btn-danger flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs"
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            <Trash2 className="w-3.5 h-3.5" />
            {t('remnawave.deleteAllIps')}
          </motion.button>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,360px)_1fr] gap-4 items-start">
        <ClientMiniMap history={details.history} nodes={details.nodes} />

        <div className="overflow-x-auto rounded-xl border border-dark-800/50">
          <table className="w-full text-sm min-w-[640px]">
            <thead>
              <tr className="border-b border-dark-800/50 text-dark-500 text-xs text-left">
                <th className="px-3 py-2 font-medium">{t('remnawave.geo.table_ip')}</th>
                <th className="px-3 py-2 font-medium">{t('remnawave.geo.table_city')}</th>
                <th className="px-3 py-2 font-medium">{t('remnawave.geo.table_provider')}</th>
                <th className="px-3 py-2 font-medium">{t('remnawave.geo.table_node')}</th>
                <th className="px-3 py-2 font-medium">{t('remnawave.geo.user_seen')}</th>
                <th className="w-8" />
              </tr>
            </thead>
            <tbody>
              {details.history.length === 0 ? (
                <tr><td colSpan={6} className="text-center py-6 text-dark-500">{t('remnawave.noData')}</td></tr>
              ) : details.history.map(ip => {
                const ipNodes = ip.nodes.map(uuid => nodesByUuid.get(uuid) ?? { uuid, name: null, country_code: null })
                const [firstNode, ...otherNodes] = ipNodes
                return (
                  <tr key={ip.source_ip} className={`border-b border-dark-800/30 last:border-0 group ${ip.online ? '' : 'text-dark-500'}`}>
                    <td className="px-3 py-2 whitespace-nowrap">
                      <span className="inline-flex items-center gap-2">
                        <span className={`w-2 h-2 rounded-full ${ip.online ? 'bg-green-400 shadow-sm shadow-green-400/50' : 'bg-dark-600'}`} />
                        <CopyableIp value={ip.source_ip} className={`font-mono ${ip.online ? 'text-dark-100' : 'text-dark-400'}`} />
                        <a
                          href={`https://check-host.net/ip-info?host=${encodeURIComponent(ip.source_ip)}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={e => e.stopPropagation()}
                          className="text-dark-500 hover:text-accent-400 transition-colors"
                        >
                          <ExternalLink className="w-3.5 h-3.5" />
                        </a>
                        <RouteButton
                          relay={!!ip.relay}
                          disabled={ipNodes.length === 0}
                          onClick={() => setRouteIp({ ip, nodes: ipNodes })}
                        />
                      </span>
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      {ip.relay ? (
                        <span className="italic text-dark-400">{t('remnawave.geo.via_relay', { name: ip.relay })}</span>
                      ) : ip.place || ip.country_code ? (
                        <span>{getFlag(ip.country_code)} {ip.place || ip.country_code}</span>
                      ) : '—'}
                    </td>
                    <td className="px-3 py-2 max-w-[200px]">
                      {ip.relay ? '—' : <ProviderBadge asOrg={ip.as_org} asn={ip.asn} />}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      {firstNode ? (
                        <span className="inline-flex items-center gap-1.5">
                          {getFlag(firstNode.country_code)} {firstNode.name ?? firstNode.uuid.slice(0, 8)}
                          {otherNodes.length > 0 && (
                            <Tooltip label={otherNodes.map(n => n.name ?? n.uuid.slice(0, 8)).join(', ')}>
                              <span className="text-xs text-dark-500 cursor-default">+{otherNodes.length}</span>
                            </Tooltip>
                          )}
                        </span>
                      ) : '—'}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap text-xs">{formatAgo(ip.last_seen, lang)}</td>
                    <td className="px-2 py-2 text-right">
                      {onDeleteIp && (
                        <Tooltip label={t('common.delete')}>
                          <button
                            onClick={e => { e.stopPropagation(); onDeleteIp(ip.source_ip) }}
                            className="opacity-60 group-hover:opacity-100 p-1.5 hover:bg-danger/20 rounded-lg text-danger transition-all"
                          >
                            <Trash2 className="w-4 h-4" />
                          </button>
                        </Tooltip>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>

      {details.devices.length > 0 && (
        <div className="space-y-1">
          <div className="flex items-center gap-1.5 text-dark-400 text-xs font-medium mb-1.5">
            <Smartphone className="w-3.5 h-3.5" />
            {t('remnawave.hwidDevices')} ({details.devices.length})
          </div>
          {details.devices.map(dev => (
            <div key={dev.hwid} className="flex items-center justify-between py-2 px-3 bg-dark-800/40 rounded-xl">
              <div className="flex items-center gap-3">
                <Smartphone className="w-3.5 h-3.5 text-dark-500" />
                <div>
                  <span className="text-dark-200 text-sm">{dev.device_model || dev.platform || 'Unknown'}</span>
                  {dev.os_version && <span className="text-dark-500 text-xs ml-2">{dev.os_version}</span>}
                </div>
              </div>
              {dev.created_at && (
                <span className="text-dark-500 text-xs">{new Date(dev.created_at).toLocaleDateString()}</span>
              )}
            </div>
          ))}
        </div>
      )}

      {routeIp && (
        <RouteModal
          ip={routeIp.ip.source_ip}
          nodes={routeIp.nodes}
          countryCode={routeIp.ip.country_code}
          place={routeIp.ip.place}
          asOrg={routeIp.ip.as_org}
          asn={routeIp.ip.asn}
          onClose={() => setRouteIp(null)}
        />
      )}
    </div>
  )
}
