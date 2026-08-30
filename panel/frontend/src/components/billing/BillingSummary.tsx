import { motion } from 'framer-motion'
import { AlertTriangle, TrendingDown, Wallet } from 'lucide-react'
import { BillingServerData } from '../../api/client'
import { Translate, formatMoneyTotals, sumByCurrency } from './shared'

const SOON_DAYS = 7

export function BillingSummary({ servers, t }: { servers: BillingServerData[]; t: Translate }) {
  const monthly = sumByCurrency(servers, s => s.monthly_cost)
  const balances = sumByCurrency(
    servers,
    s => (s.billing_type === 'monthly' ? null : s.account_balance),
  )
  const expiringSoon = servers.filter(s => s.days_left !== null && s.days_left <= SOON_DAYS)
  const expired = expiringSoon.filter(s => (s.days_left ?? 0) <= 0).length

  const tiles = [
    {
      key: 'monthly',
      icon: <TrendingDown className="w-4 h-4 text-blue-400" />,
      iconBg: 'bg-blue-500/15',
      label: t('billing.summary_monthly'),
      value: formatMoneyTotals(monthly),
      valueClass: 'text-white',
      hint: t('billing.summary_monthly_hint'),
    },
    {
      key: 'balance',
      icon: <Wallet className="w-4 h-4 text-purple-400" />,
      iconBg: 'bg-purple-500/15',
      label: t('billing.summary_balance'),
      value: formatMoneyTotals(balances),
      valueClass: 'text-white',
      hint: t('billing.summary_balance_hint'),
    },
    {
      key: 'expiring',
      icon: <AlertTriangle className={`w-4 h-4 ${expiringSoon.length > 0 ? 'text-yellow-400' : 'text-dark-500'}`} />,
      iconBg: expiringSoon.length > 0 ? 'bg-yellow-500/15' : 'bg-dark-800',
      label: t('billing.summary_expiring', { days: SOON_DAYS }),
      value: String(expiringSoon.length),
      valueClass: expiringSoon.length > 0 ? 'text-yellow-400' : 'text-dark-400',
      hint: expired > 0 ? t('billing.summary_expired', { count: expired }) : t('billing.summary_expiring_hint'),
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
          <div className={`mt-2 text-lg font-bold tabular-nums ${tile.valueClass}`}>{tile.value}</div>
          <div className="text-[11px] text-dark-500 mt-0.5">{tile.hint}</div>
        </div>
      ))}
    </motion.div>
  )
}
