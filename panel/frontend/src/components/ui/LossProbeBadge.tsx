import { useTranslation } from 'react-i18next'
import type { LossProbeStats } from '../../api/client'
import { Tooltip } from './Tooltip'

// До 2% — фон любой сети, выше 10% — адрес уже заметно деградирует для клиентов
export const LOSS_WARN_PCT = 2
export const LOSS_BAD_PCT = 10

function lossClass(lossPct: number): string {
  if (lossPct > LOSS_BAD_PCT) return 'bg-danger/10 text-danger border-danger/20'
  if (lossPct > LOSS_WARN_PCT) return 'bg-warning/10 text-warning border-warning/20'
  return 'bg-success/10 text-success border-success/20'
}

export default function LossProbeBadge({ probe }: { probe?: LossProbeStats | null }) {
  const { t } = useTranslation()
  if (!probe) return <span className="text-dark-500">—</span>
  return (
    <Tooltip label={t('loss_probe.tooltip', { samples: probe.samples })} maxWidth={280}>
      <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
        <span className={`px-2 py-0.5 rounded text-xs font-medium border ${lossClass(probe.loss_pct)}`}>
          {probe.loss_pct}%
        </span>
        {probe.rtt_ms != null && (
          <span className="text-xs text-dark-400">{Math.round(probe.rtt_ms)} {t('loss_probe.ms')}</span>
        )}
      </span>
    </Tooltip>
  )
}
