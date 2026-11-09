import { useState } from 'react'
import { motion } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { Activity, AlertTriangle, CheckCircle2, Clock, Loader2, Pencil, Stethoscope, Trash2, XCircle } from 'lucide-react'
import { greApi, type GreCheckResult, type GreTunnelView } from '../../api/client'
import { CopyableIp } from '../ui/CopyableIp'
import { formatBytes } from '../../utils/format'
import { apiErrorMessage } from '../../utils/gre'

interface Props {
  serverId: number
  tunnel: GreTunnelView
  editable: boolean
  busy: boolean
  onEdit: () => void
  onDelete: () => void
}

const LINK_STYLE: Record<string, string> = {
  ok: 'bg-success/10 text-success border-success/20',
  drift: 'bg-warning/10 text-warning border-warning/20',
  missing: 'bg-warning/10 text-warning border-warning/20',
  no_local: 'bg-danger/10 text-danger border-danger/20',
  conflict: 'bg-danger/10 text-danger border-danger/20',
}

function minutesSince(iso: string | null): number | null {
  if (!iso) return null
  return Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 60000))
}

export default function GreTunnelCard({ serverId, tunnel, editable, busy, onEdit, onDelete }: Props) {
  const { t } = useTranslation()
  const [checking, setChecking] = useState(false)
  const [check, setCheck] = useState<GreCheckResult | null>(null)

  const runCheck = async () => {
    setChecking(true)
    try {
      setCheck((await greApi.checkTunnel(serverId, tunnel.slot)).data)
    } catch (err) {
      toast.error(apiErrorMessage(err, t('gre.check_error')))
    } finally {
      setChecking(false)
    }
  }

  const minutes = minutesSince(tunnel.last_rx_at)
  const activity = (() => {
    if (tunnel.activity === 'alive') {
      return {
        icon: <Activity className="w-4 h-4 text-success" />,
        text: minutes ? t('gre.activity_alive_ago', { count: minutes }) : t('gre.activity_alive'),
      }
    }
    if (tunnel.activity === 'silent') {
      return {
        icon: <AlertTriangle className="w-4 h-4 text-danger" />,
        text: minutes !== null ? t('gre.activity_silent_ago', { count: minutes }) : t('gre.activity_never'),
      }
    }
    return { icon: <Clock className="w-4 h-4 text-dark-400" />, text: t('gre.activity_waiting') }
  })()

  const problem = tunnel.link === 'conflict'
    ? t('gre.problem_conflict', { name: tunnel.conflict_with ?? '?' })
    : tunnel.link === 'no_local'
      ? t('gre.problem_no_local', { address: tunnel.local })
      : null

  const row = (label: string, value: JSX.Element | string) => (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span className="text-xs text-dark-500">{label}</span>
      <span className="font-mono text-sm text-dark-200 text-right">{value}</span>
    </div>
  )

  return (
    <motion.div className="card" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <span className="font-mono text-sm text-dark-300">{tunnel.name}</span>
        <span className={`text-2xs px-2 py-0.5 rounded-full border ${LINK_STYLE[tunnel.link ?? ''] ?? 'bg-dark-800 text-dark-400 border-dark-700'}`}>
          {t(`gre.link_${tunnel.link ?? 'pending'}`)}
        </span>
        <div className="ml-auto flex items-center gap-1">
          <button type="button" className="btn-tool btn-tool-accent" disabled={checking || tunnel.link !== 'ok'} onClick={runCheck}>
            {checking ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Stethoscope className="w-3.5 h-3.5" />}
            {t('gre.check')}
          </button>
          {editable && (
            <>
              <button type="button" className="btn-tool" disabled={busy} onClick={onEdit} aria-label={t('common.edit')}>
                <Pencil className="w-3.5 h-3.5" />
              </button>
              <button type="button" className="btn-tool btn-tool-danger" disabled={busy} onClick={onDelete} aria-label={t('common.delete')}>
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </>
          )}
        </div>
      </div>

      <div className="mb-3">
        <div className="text-xs text-dark-500">{t('gre.ce_label')}</div>
        <CopyableIp value={tunnel.ce} className="font-mono text-xl text-dark-50" />
      </div>

      <div className="grid gap-x-6 sm:grid-cols-2">
        <div>
          {row('PE', tunnel.pe)}
          {row(t('gre.health_net'), tunnel.health_net)}
          {row('MTU', tunnel.mtu ? String(tunnel.mtu) : '—')}
        </div>
        <div>
          {row('GRE SRC', tunnel.local)}
          {row('GRE DST', tunnel.remote)}
          {row(t('gre.traffic'), `↓ ${formatBytes(tunnel.rx_bytes)} · ↑ ${formatBytes(tunnel.tx_bytes)}`)}
        </div>
      </div>

      <div className="mt-3 flex items-center gap-2 text-sm text-dark-300">
        {activity.icon}
        <span>{activity.text}</span>
      </div>

      {problem && (
        <div className="mt-3 flex items-start gap-2 px-3 py-2 rounded-xl bg-danger/10 border border-danger/20 text-xs text-dark-300">
          <XCircle className="w-4 h-4 text-danger shrink-0 mt-0.5" />
          <span>{problem}</span>
        </div>
      )}

      {check && (
        <div className="mt-3 space-y-1.5 px-3 py-2 rounded-xl bg-dark-900/50 border border-dark-700/50 text-xs">
          <div className="flex items-center gap-2">
            {check.ping_received > 0 ? <CheckCircle2 className="w-4 h-4 text-success" /> : <XCircle className="w-4 h-4 text-danger" />}
            <span className="text-dark-300">
              {t('gre.check_ping', { received: check.ping_received, sent: check.ping_sent })}
              {check.rtt_ms !== null && ` · ${check.rtt_ms.toFixed(1)} ${t('gre.ms')}`}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {check.https_ok && check.external_ip === tunnel.ce
              ? <CheckCircle2 className="w-4 h-4 text-success" />
              : check.https_ok ? <AlertTriangle className="w-4 h-4 text-warning" /> : <XCircle className="w-4 h-4 text-danger" />}
            <span className="text-dark-300">
              {check.https_ok
                ? check.external_ip === tunnel.ce
                  ? t('gre.check_https_ok', { ip: check.external_ip })
                  : t('gre.check_https_other', { ip: check.external_ip })
                : t('gre.check_https_failed', { error: check.https_error ?? '—' })}
            </span>
          </div>
        </div>
      )}
    </motion.div>
  )
}
