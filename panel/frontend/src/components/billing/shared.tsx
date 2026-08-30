import { useCallback, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { CalendarClock } from 'lucide-react'
import { BillingServerData } from '../../api/client'
import { useSettingsStore } from '../../stores/settingsStore'
import { FAQIcon, type FAQScreen } from '../FAQ'

export const MS_PER_DAY = 86_400_000
export const QUICK_DAYS = [7, 14, 30, 60, 90]

export const INPUT_CLASS =
  'w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200 ' +
  'placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition'

export type Translate = (k: string, opts?: Record<string, unknown>) => string

export function currencySymbol(currency: string): string {
  switch (currency) {
    case 'RUB': return '₽'
    case 'USD': return '$'
    case 'EUR': return '€'
    default: return currency
  }
}

export function sortServers(a: BillingServerData, b: BillingServerData) {
  const da = a.days_left ?? 9999
  const db = b.days_left ?? 9999
  return da - db
}

export function statusColor(daysLeft: number | null): string {
  if (daysLeft === null) return 'text-dark-500'
  if (daysLeft <= 3) return 'text-red-400'
  if (daysLeft <= 7) return 'text-yellow-400'
  return 'text-emerald-400'
}

export function barColor(daysLeft: number | null): string {
  if (daysLeft === null) return 'bg-dark-600'
  if (daysLeft <= 3) return 'bg-red-500'
  if (daysLeft <= 7) return 'bg-yellow-500'
  return 'bg-emerald-500'
}

export function formatDays(days: number | null, t: Translate): string {
  if (days === null) return '—'
  if (days <= 0) return t('billing.expired')
  const totalHours = Math.round(days * 24)
  const wholeDays = Math.floor(totalHours / 24)
  const hours = totalHours % 24
  if (wholeDays === 0) return `${hours}${t('billing.short_hours')}`
  if (hours === 0) return `${wholeDays}${t('billing.short_days')}`
  return `${wholeDays}${t('billing.short_days')} ${hours}${t('billing.short_hours')}`
}

/** Суммы по валютам: смешивать рубли с долларами в одно число нельзя */
export function sumByCurrency(
  servers: BillingServerData[],
  pick: (s: BillingServerData) => number | null,
): Map<string, number> {
  const totals = new Map<string, number>()
  for (const s of servers) {
    const value = pick(s)
    if (value === null || value === 0) continue
    totals.set(s.currency, (totals.get(s.currency) ?? 0) + value)
  }
  return totals
}

export function formatMoneyTotals(totals: Map<string, number>): string {
  if (totals.size === 0) return '—'
  return [...totals.entries()]
    .map(([currency, value]) => `${value.toFixed(2)} ${currencySymbol(currency)}`)
    .join(' · ')
}

export function useBillingDateFormat() {
  const tz = useSettingsStore(s => s.getEffectiveTimezone)()

  const formatDate = useCallback((isoDate: string) => {
    try {
      return new Date(isoDate).toLocaleDateString(undefined, {
        timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
      })
    } catch {
      return new Date(isoDate).toLocaleDateString()
    }
  }, [tz])

  const formatDateTime = useCallback((isoDate: string) => {
    try {
      return new Date(isoDate).toLocaleString(undefined, {
        timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit',
      })
    } catch {
      return new Date(isoDate).toLocaleString()
    }
  }, [tz])

  return { formatDate, formatDateTime }
}

export function Overlay({ children, onClose }: { children: React.ReactNode; onClose: () => void }) {
  const mouseDownTarget = useRef<EventTarget | null>(null)

  return (
    <AnimatePresence>
      <motion.div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onMouseDown={e => { mouseDownTarget.current = e.target }}
        onClick={e => {
          if (e.target === e.currentTarget && mouseDownTarget.current === e.currentTarget) onClose()
        }}
      >
        <motion.div
          initial={{ opacity: 0, scale: 0.95, y: 20 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.95, y: 20 }}
          transition={{ duration: 0.2 }}
          className="bg-dark-900 border border-dark-800 rounded-2xl shadow-2xl w-full max-w-md max-h-[90vh] overflow-y-auto"
          onClick={e => e.stopPropagation()}
        >
          {children}
        </motion.div>
      </motion.div>
    </AnimatePresence>
  )
}

export function Field({ label, children, faqScreen }: {
  label: string
  children: React.ReactNode
  faqScreen?: FAQScreen
}) {
  return (
    <div className="space-y-1.5">
      <label className="text-sm text-dark-300 flex items-center gap-1">
        {label}
        {faqScreen && <FAQIcon screen={faqScreen} size="sm" />}
      </label>
      {children}
    </div>
  )
}

export function ToggleRow({ label, checked, onChange }: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-sm text-dark-300">{label}</span>
      <button
        onClick={() => onChange(!checked)}
        className={`relative w-10 h-5 rounded-full transition-colors ${checked ? 'bg-accent-500' : 'bg-dark-700'}`}
      >
        <motion.div
          className="absolute top-0.5 w-4 h-4 bg-white rounded-full shadow"
          animate={{ left: checked ? 22 : 2 }}
          transition={{ type: 'spring', stiffness: 500, damping: 30 }}
        />
      </button>
    </div>
  )
}

export function PaidTotalHint({ totalDays, t, formatDateTime, labelKey = 'billing.total_paid' }: {
  totalDays: number
  t: Translate
  formatDateTime: (iso: string) => string
  labelKey?: string
}) {
  const paidUntil = new Date(Date.now() + totalDays * MS_PER_DAY).toISOString()
  return (
    <div className="flex items-center gap-1 flex-wrap">
      <CalendarClock className="w-3 h-3" />
      {t(labelKey)}: <span className="font-semibold">{formatDays(totalDays, t)}</span>
      <span className="opacity-70">· {t('billing.until')} {formatDateTime(paidUntil)}</span>
    </div>
  )
}
