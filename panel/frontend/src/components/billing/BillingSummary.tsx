import { motion } from 'framer-motion'
import { AlertTriangle, TrendingDown, Wallet } from 'lucide-react'
import { BillingServerData } from '../../api/client'
import {
  Translate, formatDays, formatMoneyTotals, sortServers, statusColor, sumByCurrency,
} from './shared'

const URGENT_DAYS = 7

type Tone = 'red' | 'yellow' | 'muted'

const TONE_ICON: Record<Tone, string> = {
  red: 'text-red-400',
  yellow: 'text-yellow-400',
  muted: 'text-dark-500',
}

const TONE_ICON_BG: Record<Tone, string> = {
  red: 'bg-red-500/15',
  yellow: 'bg-yellow-500/15',
  muted: 'bg-dark-800',
}

function expiredLabel(count: number, t: Translate): string {
  const label = t('billing.expired')
  return count > 1 ? `${label} (${count})` : label
}

function dueTone(expired: number, nearest: BillingServerData | null): Tone {
  if (expired > 0) return 'red'
  if (nearest !== null && (nearest.days_left ?? 0) <= URGENT_DAYS) return 'yellow'
  return 'muted'
}

export function BillingSummary({ servers, t, formatDateTime }: {
  servers: BillingServerData[]
  t: Translate
  formatDateTime: (iso: string) => string
}) {
  const monthly = sumByCurrency(servers, s => s.monthly_cost)
  const balances = sumByCurrency(
    servers,
    s => (s.billing_type === 'monthly' ? null : s.account_balance),
  )
  const expired = servers.filter(s => s.days_left !== null && s.days_left <= 0).length
  const nearest = servers
    .filter(s => s.days_left !== null && s.days_left > 0)
    .sort(sortServers)[0] ?? null
  const nearestDays = nearest?.days_left ?? null
  const tone = dueTone(expired, nearest)

  const dueValue = expired > 0
    ? expiredLabel(expired, t)
    : formatDays(nearestDays, t)
  const dueAside = expired > 0 && nearest
    ? t('billing.summary_nearest_in', { value: formatDays(nearestDays, t) })
    : null
  const dueHint = nearest
    ? [nearest.name, nearest.paid_until && formatDateTime(nearest.paid_until)].filter(Boolean).join(' · ')
    : t('billing.summary_no_upcoming')

  const tiles = [
    {
      key: 'monthly',
      icon: <TrendingDown className="w-4 h-4 text-blue-400" />,
      iconBg: 'bg-blue-500/15',
      label: t('billing.summary_monthly'),
      value: formatMoneyTotals(monthly),
      valueClass: 'text-white',
      aside: null,
      asideClass: '',
      hint: t('billing.summary_monthly_hint'),
    },
    {
      key: 'balance',
      icon: <Wallet className="w-4 h-4 text-purple-400" />,
      iconBg: 'bg-purple-500/15',
      label: t('billing.summary_balance'),
      value: formatMoneyTotals(balances),
      valueClass: 'text-white',
      aside: null,
      asideClass: '',
      hint: t('billing.summary_balance_hint'),
    },
    {
      key: 'due',
      icon: <AlertTriangle className={`w-4 h-4 ${TONE_ICON[tone]}`} />,
      iconBg: TONE_ICON_BG[tone],
      label: t('billing.summary_due'),
      value: dueValue,
      valueClass: expired > 0 ? 'text-red-400' : statusColor(nearestDays),
      aside: dueAside,
      asideClass: statusColor(nearestDays),
      hint: dueHint,
    },
  ]

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="grid gap-3 sm:grid-cols-3"
    >
      {tiles.map(tile => (
        <div key={tile.key} className="bg-dark-900/50 rounded-xl border border-dark-800/50 p-4">
          <div className="flex items-center gap-2">
            <div className={`w-7 h-7 rounded-lg flex items-center justify-center ${tile.iconBg}`}>
              {tile.icon}
            </div>
            <span className="text-xs text-dark-400">{tile.label}</span>
          </div>
          <div className="mt-2 flex items-baseline gap-2 flex-wrap">
            <span className={`text-lg font-bold tabular-nums ${tile.valueClass}`}>{tile.value}</span>
            {tile.aside && (
              <span className={`text-xs font-medium tabular-nums ${tile.asideClass}`}>{tile.aside}</span>
            )}
          </div>
          <div className="text-[11px] text-dark-500 mt-0.5 truncate">{tile.hint}</div>
        </div>
      ))}
    </motion.div>
  )
}
