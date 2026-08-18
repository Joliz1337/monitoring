import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { Gauge, Loader2, CheckCircle2, AlertTriangle } from 'lucide-react'
import { proxyApi, BandwidthLimitState, type Server } from '../../api/client'
import { nodeAllows } from '../../utils/nodeCapabilities'
import { Tooltip } from '../ui/Tooltip'

const MIN_MBIT = 1
const MAX_MBIT = 100_000

interface Props {
  serverId: number
  server?: Server | null
}

/** Искусственный лимит полосы ноды (tc cake/tbf), ставится и восстанавливается агентом. */
export default function BandwidthLimitCard({ serverId, server }: Props) {
  const { t } = useTranslation()
  const writable = nodeAllows(server, 'system', 'write')
  const readable = nodeAllows(server, 'system', 'read')

  const [state, setState] = useState<BandwidthLimitState | null>(null)
  const [unsupported, setUnsupported] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [enabled, setEnabled] = useState(false)
  const [mbitText, setMbitText] = useState('')

  const load = useCallback(async () => {
    if (!readable) {
      setLoading(false)
      return
    }
    try {
      const res = await proxyApi.getBandwidthLimit(serverId)
      setState(res.data)
      setEnabled(res.data.enabled)
      setMbitText(res.data.mbit ? String(res.data.mbit) : '')
      setUnsupported(false)
    } catch (err) {
      if ((err as { response?: { status?: number } }).response?.status === 404) setUnsupported(true)
    } finally {
      setLoading(false)
    }
  }, [serverId, readable])

  useEffect(() => {
    load()
  }, [load])

  const apply = async () => {
    const mbit = parseInt(mbitText, 10)
    if (enabled && (Number.isNaN(mbit) || mbit < MIN_MBIT || mbit > MAX_MBIT)) {
      toast.error(t('server_details.bandwidth_validation'))
      return
    }
    setSaving(true)
    try {
      const res = await proxyApi.setBandwidthLimit(serverId, { enabled, mbit: enabled ? mbit : 0 })
      setState(res.data)
      toast.success(enabled ? t('server_details.bandwidth_applied') : t('server_details.bandwidth_removed'))
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
      toast.error(detail || t('server_details.bandwidth_error'))
    } finally {
      setSaving(false)
    }
  }

  if (!readable || unsupported) {
    return (
      <div className="card">
        <h3 className="font-semibold text-dark-100 mb-2 flex items-center gap-2">
          <Gauge className="w-4 h-4 text-accent-500" />
          {t('server_details.bandwidth_title')}
        </h3>
        <p className="text-sm text-dark-500">
          {unsupported ? t('server_details.bandwidth_unsupported') : t('node_caps.row_blocked')}
        </p>
      </div>
    )
  }

  const statusLine = () => {
    if (!state) return null
    if (!state.enabled) return <span className="text-dark-500">{t('server_details.bandwidth_state_off')}</span>
    if (state.in_sync && state.applied) {
      return (
        <span className="text-success inline-flex items-center gap-1.5">
          <CheckCircle2 className="w-4 h-4" />
          {t('server_details.bandwidth_state_on', { mbit: state.applied_mbit ?? state.mbit, qdisc: state.qdisc, iface: state.iface })}
        </span>
      )
    }
    return (
      <span className="text-warning inline-flex items-center gap-1.5">
        <AlertTriangle className="w-4 h-4" />
        {t('server_details.bandwidth_state_drift', { mbit: state.mbit })}
      </span>
    )
  }

  return (
    <div className="card">
      <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
        <h3 className="font-semibold text-dark-100 flex items-center gap-2">
          <Gauge className="w-4 h-4 text-accent-500" />
          {t('server_details.bandwidth_title')}
          <Tooltip label={t('server_details.bandwidth_hint')} maxWidth={380}>
            <span className="text-dark-500 text-xs font-normal cursor-help">?</span>
          </Tooltip>
        </h3>
        <div className="text-sm">{loading ? <Loader2 className="w-4 h-4 animate-spin text-dark-500" /> : statusLine()}</div>
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <label className="inline-flex items-center gap-2 text-sm text-dark-300 cursor-pointer">
          <input
            type="checkbox"
            checked={enabled}
            disabled={!writable || loading}
            onChange={e => setEnabled(e.target.checked)}
            className="accent-accent-500"
          />
          {t('server_details.bandwidth_enable')}
        </label>
        <div className="flex items-center gap-2">
          <input
            type="number"
            min={MIN_MBIT}
            max={MAX_MBIT}
            value={mbitText}
            disabled={!writable || !enabled || loading}
            onChange={e => setMbitText(e.target.value)}
            placeholder="950"
            className="w-28 px-3 py-1.5 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm placeholder-dark-600 focus:outline-none focus:border-accent-500/50 transition-colors disabled:opacity-50"
          />
          <span className="text-sm text-dark-400">{t('server_details.bandwidth_mbit')}</span>
        </div>
        <button
          onClick={apply}
          disabled={!writable || saving || loading}
          className="px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50 flex items-center gap-1.5"
        >
          {saving && <Loader2 className="w-3 h-3 animate-spin" />}
          {t('server_details.bandwidth_apply')}
        </button>
      </div>
    </div>
  )
}
