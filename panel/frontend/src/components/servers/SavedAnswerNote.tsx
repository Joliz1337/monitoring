import { useTranslation } from 'react-i18next'
import { WifiOff } from 'lucide-react'
import { formatDateLocalized } from '../Charts/chartTheme'

// Так панель отвечает (409), когда нода молчит, а копии её ответа в базе нет
const ANSWER_COPY_MISSING_HEADER = 'x-node-answer-copy-missing'

export function isAnswerCopyMissing(error: unknown): boolean {
  const headers = (error as { response?: { headers?: Record<string, unknown> } }).response?.headers
  return headers?.[ANSWER_COPY_MISSING_HEADER] !== undefined
}

export function formatMoment(iso: string, lang: string): string {
  return formatDateLocalized(new Date(iso), 'dd MMM yyyy, HH:mm', lang)
}

/** Нода не отвечает: карточка показывает её последний ответ, сохранённый панелью. */
export function SavedAnswerNote({ savedAt, fromMetrics = false }: { savedAt: string; fromMetrics?: boolean }) {
  const { t, i18n } = useTranslation()
  const key = fromMetrics ? 'node_silent.saved_from_metrics' : 'node_silent.saved_answer'
  return (
    <p className="text-xs text-warning flex items-start gap-1.5 mb-3">
      <WifiOff className="w-3.5 h-3.5 shrink-0 mt-0.5" />
      <span>{t(key, { time: formatMoment(savedAt, i18n.language) })}</span>
    </p>
  )
}

/** Нода не отвечает, и показать за неё нечего. */
export function NoSavedAnswerNote() {
  const { t } = useTranslation()
  return (
    <p className="text-sm text-dark-500 flex items-start gap-1.5">
      <WifiOff className="w-4 h-4 shrink-0 mt-0.5" />
      <span>{t('node_silent.no_copy')}</span>
    </p>
  )
}
