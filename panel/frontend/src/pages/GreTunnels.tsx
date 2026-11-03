import { useCallback, useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { AlertTriangle, ArrowLeft, CheckCircle2, Clock, Loader2, Plus, RefreshCw, Waypoints, XCircle } from 'lucide-react'
import {
  greApi,
  proxyApi,
  type GreInstallStatus,
  type GreNodeView,
  type GreTunnelInput,
  type GreTunnelView,
} from '../api/client'
import { useServersStore } from '../stores/serversStore'
import NodeRestrictedNotice from '../components/servers/NodeRestrictedNotice'
import GreTunnelCard from '../components/gre/GreTunnelCard'
import GreTunnelForm from '../components/gre/GreTunnelForm'
import { nodeAllows } from '../utils/nodeCapabilities'
import { apiErrorMessage } from '../utils/gre'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import { Tooltip } from '../components/ui/Tooltip'
import { Toggle } from '../components/ui/Toggle'
import { FAQIcon } from '../components/FAQ'

const REFRESH_INTERVAL_MS = 10000

// null — форма закрыта, 'new' — новый туннель, число — правка туннеля с этим номером
type Editing = null | 'new' | number

function toInput(tunnel: GreTunnelView): GreTunnelInput {
  return {
    slot: tunnel.slot, local: tunnel.local, remote: tunnel.remote,
    pe: tunnel.pe, ce: tunnel.ce, health_net: tunnel.health_net,
  }
}

const STATUS_ICON: Record<GreInstallStatus, { icon: JSX.Element; border: string }> = {
  active: { icon: <CheckCircle2 className="w-6 h-6 text-success" />, border: 'border-success/30' },
  drift: { icon: <AlertTriangle className="w-6 h-6 text-warning" />, border: 'border-warning/30' },
  pending: { icon: <Clock className="w-6 h-6 text-accent-400" />, border: 'border-accent-500/30' },
  off: { icon: <CheckCircle2 className="w-6 h-6 text-dark-400" />, border: 'border-dark-700/50' },
  failed: { icon: <XCircle className="w-6 h-6 text-danger" />, border: 'border-danger/30' },
  denied: { icon: <XCircle className="w-6 h-6 text-danger" />, border: 'border-danger/30' },
  unsupported: { icon: <XCircle className="w-6 h-6 text-danger" />, border: 'border-danger/30' },
}

export default function GreTunnels() {
  const { uid, serverId } = useParams()
  const navigate = useNavigate()
  const { t } = useTranslation()
  const { servers, fetchServers } = useServersStore()
  const server = servers.find(s => s.id === Number(serverId))
  const systemWritable = nodeAllows(server, 'system', 'write')

  const [view, setView] = useState<GreNodeView | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<Editing>(null)
  const [serverAddresses, setServerAddresses] = useState<string[] | null>(null)

  const fetchView = useCallback(async () => {
    if (!serverId) return
    setIsRefreshing(true)
    try {
      setView((await greApi.getNode(Number(serverId))).data)
      setError(null)
    } catch (err) {
      setError(apiErrorMessage(err, t('gre.load_error')))
    } finally {
      setIsLoading(false)
      setIsRefreshing(false)
    }
  }, [serverId, t])

  useEffect(() => {
    fetchServers()
  }, [fetchServers])

  useAutoRefresh(fetchView, { customInterval: REFRESH_INTERVAL_MS })

  // Адреса интерфейсов для GRE SRC: основной первым
  useEffect(() => {
    if (!serverId) return
    proxyApi.getNetworkState(Number(serverId))
      .then(res => {
        const addresses = res.data.interfaces
          .flatMap(iface => iface.addresses)
          .filter(address => address.family === 'ipv4' && address.scope === 'global')
          .sort((a, b) => Number(b.primary) - Number(a.primary))
          .map(address => address.address)
        setServerAddresses(Array.from(new Set(addresses)))
      })
      .catch(() => setServerAddresses(null))
  }, [serverId])

  const handleRefresh = async () => {
    if (!serverId || !view?.tunnels.length) {
      await fetchView()
      return
    }
    setIsRefreshing(true)
    try {
      setView((await greApi.refreshNode(Number(serverId))).data)
      setError(null)
    } catch (err) {
      toast.error(apiErrorMessage(err, t('gre.load_error')))
    } finally {
      setIsRefreshing(false)
    }
  }

  const save = async (patch: { tunnels?: GreTunnelInput[]; notify?: boolean }): Promise<boolean> => {
    if (!serverId) return false
    setSaving(true)
    try {
      const res = await greApi.updateNode(Number(serverId), patch)
      setView(res.data)
      if (res.data.sync_error) toast.error(res.data.sync_error)
      else toast.success(t('gre.saved'))
      return true
    } catch (err) {
      toast.error(apiErrorMessage(err, t('gre.save_error')))
      return false
    } finally {
      setSaving(false)
    }
  }

  const saveTunnel = async (tunnel: GreTunnelInput) => {
    if (!view) return
    const others = view.tunnels.filter(item => item.slot !== tunnel.slot).map(toInput)
    if (await save({ tunnels: [...others, tunnel] })) setEditing(null)
  }

  const deleteTunnel = (tunnel: GreTunnelView) => {
    if (!view || !confirm(t('gre.delete_confirm', { ce: tunnel.ce }))) return
    save({ tunnels: view.tunnels.filter(item => item.slot !== tunnel.slot).map(toInput) })
  }

  const canConfigure = systemWritable && !!view && view.supported_by_node
  const status = view ? STATUS_ICON[view.install_status] : null
  const statusHint = view && ['failed', 'denied', 'unsupported', 'drift', 'pending'].includes(view.install_status)
    ? view.sync_error ?? view.node_error
    : null
  const editingTunnel = typeof editing === 'number' ? view?.tunnels.find(item => item.slot === editing) : undefined

  return (
    <div>
      <motion.div
        className="flex items-center gap-4 mb-6"
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
      >
        <Tooltip label={t('common.back')}>
          <motion.button
            onClick={() => navigate(`/${uid}/server/${serverId}`)}
            className="p-2.5 hover:bg-dark-800 rounded-xl text-dark-400 hover:text-dark-200 transition-all"
            whileHover={{ scale: 1.05, x: -2 }}
            whileTap={{ scale: 0.95 }}
          >
            <ArrowLeft className="w-5 h-5" />
          </motion.button>
        </Tooltip>
        <div className="flex-1">
          <motion.h1
            className="text-2xl font-bold text-dark-50 flex items-center gap-3"
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
          >
            <Waypoints className="w-7 h-7 text-accent-400" />
            {t('gre.title')}
            <FAQIcon screen="PAGE_GRE" />
            {isRefreshing && <Loader2 className="w-5 h-5 text-accent-400 animate-spin" />}
          </motion.h1>
          <motion.p className="text-dark-400 mt-1" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.1 }}>
            {server?.name} · {t('gre.subtitle')}
          </motion.p>
        </div>
        <Tooltip label={t('common.refresh_data')}>
          <motion.button
            onClick={handleRefresh}
            disabled={isRefreshing}
            className="p-2.5 hover:bg-dark-800 rounded-xl text-dark-400 hover:text-dark-200 transition-all disabled:opacity-50"
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
          >
            <RefreshCw className={`w-5 h-5 ${isRefreshing ? 'animate-spin' : ''}`} />
          </motion.button>
        </Tooltip>
      </motion.div>

      {!systemWritable && <div className="mb-4"><NodeRestrictedNotice server={server} variant="readonly" /></div>}

      {isLoading ? (
        <div className="flex items-center justify-center h-48">
          <Loader2 className="w-6 h-6 animate-spin text-dark-500" />
        </div>
      ) : error ? (
        <div className="card text-center py-12">
          <XCircle className="w-12 h-12 text-danger/50 mx-auto mb-3" />
          <p className="text-danger">{error}</p>
        </div>
      ) : view && (
        <div className="space-y-4">
          {!view.supported_by_node && (
            <div className="flex items-start gap-3 px-4 py-3 rounded-xl bg-warning/10 border border-warning/20">
              <AlertTriangle className="w-5 h-5 text-warning shrink-0 mt-0.5" />
              <div className="text-sm">
                <div className="text-dark-100 font-medium">{t('gre.node_too_old')}</div>
                <div className="text-dark-400 text-xs mt-0.5">{t('gre.node_too_old_hint', { version: view.min_node_version })}</div>
              </div>
            </div>
          )}

          <motion.div className={`card border ${status?.border}`} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
            <div className="flex items-start gap-4">
              <div className="shrink-0 mt-0.5">{status?.icon}</div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between gap-4">
                  <div className="text-sm">
                    <span className="font-medium text-dark-100">{t(`gre.status_${view.install_status}`)}</span>
                    {view.tunnels.length > 0 && (
                      <span className="text-dark-400"> · {t('gre.tunnel_count', { count: view.tunnels.length })}</span>
                    )}
                    {statusHint && <div className="text-xs text-dark-400 mt-0.5 break-words">{statusHint}</div>}
                  </div>
                  <div className="flex items-center gap-3 shrink-0">
                    <span className="text-xs text-dark-400 hidden sm:inline">{t('gre.notify')}</span>
                    <Toggle
                      on={view.notify}
                      disabled={saving}
                      onClick={() => save({ notify: !view.notify })}
                      title={t('gre.notify_hint', { minutes: Math.round(view.silent_after_seconds / 60) })}
                    />
                  </div>
                </div>
              </div>
            </div>
            <p className="text-xs text-dark-500 mt-4 leading-relaxed">{t('gre.how_it_works')}</p>
          </motion.div>

          {view.tunnels.map(tunnel => editing === tunnel.slot && editingTunnel ? (
            <GreTunnelForm
              key={tunnel.slot}
              initial={toInput(editingTunnel)}
              serverAddresses={serverAddresses}
              saving={saving}
              onSave={saveTunnel}
              onCancel={() => setEditing(null)}
            />
          ) : (
            <GreTunnelCard
              key={tunnel.slot}
              serverId={view.server_id}
              tunnel={tunnel}
              editable={canConfigure}
              busy={saving || editing !== null}
              onEdit={() => setEditing(tunnel.slot)}
              onDelete={() => deleteTunnel(tunnel)}
            />
          ))}

          {editing === 'new' ? (
            <GreTunnelForm
              initial={{ local: '', remote: view.default_remote, ce: '', pe: null, health_net: view.default_health_net }}
              serverAddresses={serverAddresses}
              saving={saving}
              onSave={saveTunnel}
              onCancel={() => setEditing(null)}
            />
          ) : canConfigure && view.tunnels.length < view.max_tunnels && editing === null && (
            <button type="button" className="btn btn-secondary w-full justify-center" onClick={() => setEditing('new')}>
              <Plus className="w-4 h-4" />
              {view.tunnels.length ? t('gre.add_another') : t('gre.add_first')}
            </button>
          )}

          {view.tunnels.length > 0 && (
            <div className="card text-xs text-dark-400 leading-relaxed">
              <div className="text-dark-200 font-medium text-sm mb-1">{t('gre.next_title')}</div>
              {t('gre.next_text')}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
