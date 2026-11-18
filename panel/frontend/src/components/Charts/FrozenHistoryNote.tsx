import { useTranslation } from 'react-i18next'
import type { HistoryResponse } from '../../api/client'
import { formatMoment } from '../servers/SavedAnswerNote'

const PERIOD_OF_SOURCE: Record<string, string> = { hour: '24h', day: '30d' }

/**
 * Период, по которому график форматирует даты. У молчащей ноды вместо запрошенного
 * часа могут прийти часовые или суточные точки: с «часовыми» подписями ось
 * показывала бы «00:00» на каждой.
 */
export function chartPeriodOf(history: HistoryResponse | null, period: string): string {
  if (!history?.coarser_than_period) return period
  return PERIOD_OF_SOURCE[history.data_source] ?? period
}

/** Подпись к графикам молчащей ноды: окно периода кончается на её последнем ответе. */
export default function FrozenHistoryNote({ history, className = '' }: { history: HistoryResponse; className?: string }) {
  const { t, i18n } = useTranslation()
  if (!history.frozen_at) return null
  const coarserKey = history.data_source === 'day'
    ? 'node_silent.history_coarser_day'
    : 'node_silent.history_coarser_hour'
  return (
    <p className={`text-xs text-warning ${className}`}>
      {t('node_silent.history_frozen', { time: formatMoment(history.frozen_at, i18n.language) })}
      {history.coarser_than_period && `. ${t(coarserKey)}`}
    </p>
  )
}
