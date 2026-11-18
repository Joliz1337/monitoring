import { useTranslation } from 'react-i18next'
import { formatMoment } from '../servers/SavedAnswerNote'

interface TrafficFrozenNoteProps {
  frozenAt: string
  /** Часовых бакетов уже нет: вместо них показаны суточные итоги */
  dailyTotals?: boolean
  className?: string
}

/**
 * Подпись к трафику молчащего сервера: его периоды отсчитаны от последнего ответа,
 * а без неё «(30д)» и «24h» читались бы как «до сегодня».
 */
export function TrafficFrozenNote({ frozenAt, dailyTotals = false, className = '' }: TrafficFrozenNoteProps) {
  const { t, i18n } = useTranslation()
  return (
    <p className={`text-xs text-warning ${className}`}>
      {t('node_silent.traffic_frozen', { time: formatMoment(frozenAt, i18n.language) })}
      {dailyTotals && `. ${t('node_silent.traffic_coarser_day')}`}
    </p>
  )
}

/** Подпись к карточке молчащей ноды: её цифры — последний ответ, а не то, что происходит сейчас. */
export function SnapshotNote({ takenAt, className = '' }: { takenAt: string; className?: string }) {
  const { t, i18n } = useTranslation()
  return (
    <p className={`text-xs text-warning ${className}`}>
      {t('node_silent.snapshot', { time: formatMoment(takenAt, i18n.language) })}
    </p>
  )
}
