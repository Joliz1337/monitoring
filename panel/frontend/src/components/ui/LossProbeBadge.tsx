import type { ReactElement } from 'react'
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

// bare — без своей подсказки: значок стоит внутри всплывающей карточки, которая говорит больше
function Hinted({ label, bare, children }: { label: string; bare: boolean; children: ReactElement }) {
  return bare ? children : <Tooltip label={label} maxWidth={280}>{children}</Tooltip>
}

export default function LossProbeBadge({ probe, bare = false }: { probe?: LossProbeStats | null; bare?: boolean }) {
  const { t } = useTranslation()
  if (!probe) return <span className="text-dark-500">—</span>
  if (probe.answers === false) {
    // Подключения доходят, поэтому процент потерь здесь обманчиво мал
    return (
      <Hinted label={t('loss_probe.no_reply_tooltip')} bare={bare}>
        <span className="px-2 py-0.5 rounded text-xs font-medium border whitespace-nowrap bg-danger/10 text-danger border-danger/20">
          {t('loss_probe.no_reply')}
        </span>
      </Hinted>
    )
  }
  return (
    <Hinted label={t('loss_probe.tooltip', { samples: probe.samples })} bare={bare}>
      <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
        <span className={`px-2 py-0.5 rounded text-xs font-medium border ${lossClass(probe.loss_pct)}`}>
          {probe.loss_pct}%
        </span>
        {probe.rtt_ms != null && (
          <span className="text-xs text-dark-400">{Math.round(probe.rtt_ms)} {t('loss_probe.ms')}</span>
        )}
      </span>
    </Hinted>
  )
}
