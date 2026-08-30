import { motion } from 'framer-motion'
import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import {
  ArrowUpCircle, CalendarClock, CalendarX2, Calculator, Cloud, DollarSign,
  GripVertical, Loader2, MoveRight, Pencil, RefreshCw, Trash2, Wallet,
} from 'lucide-react'
import { BillingServerData } from '../../api/client'
import { Tooltip } from '../ui/Tooltip'
import { getProvider } from './providers'
import {
  Translate, barColor, currencySymbol, formatDays, statusColor,
} from './shared'

const MAX_BAR_DAYS = 30

export function ProjectCard({
  server, index, t, formatDateTime, sortable, syncing,
  onExtend, onTopup, onEdit, onDelete, onMoveToFolder, onSync, onPlan,
}: {
  server: BillingServerData
  index: number
  t: Translate
  formatDateTime: (iso: string) => string
  sortable?: boolean
  syncing?: boolean
  onExtend: () => void
  onTopup: () => void
  onEdit: () => void
  onDelete: () => void
  onMoveToFolder: () => void
  onSync: () => void
  onPlan: () => void
}) {
  const {
    setNodeRef, setActivatorNodeRef, attributes, listeners, transform, transition, isDragging,
  } = useSortable({ id: server.id, disabled: !sortable })

  const style: React.CSSProperties = {
    ...(sortable ? {
      transform: CSS.Transform.toString(transform),
      transition,
      opacity: isDragging ? 0.3 : 1,
    } : {}),
    // Виртуализация без удаления из DOM: офф-скрин карточки не рендерятся (важно на большом флоте).
    contentVisibility: isDragging ? 'visible' : 'auto',
    containIntrinsicSize: 'auto 200px',
  }

  const isCloud = server.billing_type === 'cloud'
  const provider = isCloud ? getProvider(server.cloud_provider) : null
  const dl = server.days_left
  const pct = dl !== null ? Math.min(100, Math.max(0, (dl / MAX_BAR_DAYS) * 100)) : 0
  const dailyCost = isCloud && server.cloud_daily_cost
    ? server.cloud_daily_cost
    : server.monthly_cost ? server.monthly_cost / 30 : null

  const iconBg = provider?.accent.iconBg
    ?? (server.billing_type === 'monthly' ? 'bg-blue-500/20' : 'bg-purple-500/20')
  const badgeClass = provider?.accent.badge
    ?? (server.billing_type === 'monthly' ? 'bg-blue-500/15 text-blue-400' : 'bg-purple-500/15 text-purple-400')
  const typeLabel = provider ? t(provider.nameKey) : t(`billing.type_${server.billing_type}`)

  return (
    <motion.div
      ref={sortable ? setNodeRef : undefined}
      style={style}
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.04 }}
      className="bg-dark-900/50 rounded-xl border border-dark-800/50 p-5"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3 min-w-0">
          {sortable && (
            <div
              ref={setActivatorNodeRef}
              {...attributes}
              {...listeners}
              className="p-1 text-dark-600 hover:text-dark-400 cursor-grab active:cursor-grabbing transition rounded flex-shrink-0"
            >
              <GripVertical className="w-4 h-4" />
            </div>
          )}
          <div className={`w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0 ${iconBg}`}>
            {provider
              ? <Cloud className={`w-5 h-5 ${provider.accent.icon}`} />
              : server.billing_type === 'monthly'
                ? <CalendarClock className="w-5 h-5 text-blue-400" />
                : <Wallet className="w-5 h-5 text-purple-400" />
            }
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-white truncate">{server.name}</h3>
              <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium uppercase ${badgeClass}`}>
                {typeLabel}
              </span>
            </div>
            <div className="flex items-center gap-3 mt-1 text-xs text-dark-400 flex-wrap">
              {(server.billing_type === 'resource' || isCloud) && server.account_balance !== null && (
                <span className="flex items-center gap-1">
                  <DollarSign className="w-3 h-3" />
                  {server.account_balance.toFixed(2)} {currencySymbol(server.currency)}
                </span>
              )}
              {dailyCost !== null && dailyCost > 0 && (
                <span className="flex items-center gap-1 text-dark-500">
                  {isCloud ? '~' : ''}{dailyCost.toFixed(2)} {currencySymbol(server.currency)}{t('billing.per_day')}
                </span>
              )}
              {isCloud && server.cloud_last_error && (
                <Tooltip label={server.cloud_last_error} maxWidth={320}>
                  <span className="text-red-400 text-[10px] truncate max-w-[150px]">
                    {t('billing.sync_error')}
                  </span>
                </Tooltip>
              )}
              {server.notes && (
                <span className="truncate max-w-[200px]">{server.notes}</span>
              )}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2 flex-shrink-0">
          <span className={`text-lg font-bold tabular-nums ${statusColor(dl)}`}>
            {formatDays(dl, t)}
          </span>
        </div>
      </div>

      {server.paid_until && (
        <div className={`mt-2.5 flex items-center gap-1.5 text-xs ${
          dl !== null && dl <= 3 ? 'text-red-400/80' : dl !== null && dl <= 7 ? 'text-yellow-400/80' : 'text-dark-400'
        }`}>
          <CalendarX2 className="w-3.5 h-3.5" />
          <span>{t('billing.expires_at')}:</span>
          <span className="font-medium">{formatDateTime(server.paid_until)}</span>
        </div>
      )}

      <div className="mt-2.5 h-1.5 bg-dark-800 rounded-full overflow-hidden">
        <motion.div
          className={`h-full rounded-full ${barColor(dl)}`}
          initial={{ width: 0 }}
          animate={{ width: `${pct}%` }}
          transition={{ duration: 0.5, ease: 'easeOut' }}
        />
      </div>

      <div className="flex items-center justify-between mt-3">
        <div className="flex gap-2">
          {provider ? (
            <>
              <button
                onClick={onSync}
                disabled={syncing}
                className={`flex items-center gap-1.5 px-4 py-2 text-xs font-semibold rounded-xl
                            transition-all disabled:opacity-60 disabled:cursor-not-allowed ${provider.accent.primaryButton}`}
              >
                {syncing
                  ? <Loader2 className="w-4 h-4 animate-spin" />
                  : <RefreshCw className="w-4 h-4" />
                }
                {t('billing.sync')}
              </button>
              <button
                onClick={onPlan}
                className={`flex items-center gap-1.5 px-4 py-2 text-xs font-semibold
                            bg-dark-800 text-dark-300 border border-dark-700/50
                            rounded-xl transition-all ${provider.accent.ghostButton}`}
              >
                <Calculator className="w-4 h-4" />
                {t('billing.plan')}
              </button>
            </>
          ) : server.billing_type === 'monthly' ? (
            <button
              onClick={onExtend}
              className="flex items-center gap-1.5 px-4 py-2 text-xs font-semibold
                         bg-gradient-to-r from-emerald-500/20 to-teal-500/20 text-emerald-400
                         hover:from-emerald-500/30 hover:to-teal-500/30
                         border border-emerald-500/20 hover:border-emerald-500/40
                         rounded-xl transition-all shadow-sm shadow-emerald-500/5"
            >
              <ArrowUpCircle className="w-4 h-4" />
              {t('billing.extend')}
            </button>
          ) : (
            <button
              onClick={onTopup}
              className="flex items-center gap-1.5 px-4 py-2 text-xs font-semibold
                         bg-gradient-to-r from-purple-500/20 to-violet-500/20 text-purple-400
                         hover:from-purple-500/30 hover:to-violet-500/30
                         border border-purple-500/20 hover:border-purple-500/40
                         rounded-xl transition-all shadow-sm shadow-purple-500/5"
            >
              <Wallet className="w-4 h-4" />
              {t('billing.topup')}
            </button>
          )}
        </div>
        <div className="flex gap-1">
          <Tooltip label={t('billing.move_to_folder')}>
            <button
              onClick={onMoveToFolder}
              className="p-1.5 text-dark-500 hover:text-blue-400 transition rounded-lg hover:bg-dark-800/50"
            >
              <MoveRight className="w-3.5 h-3.5" />
            </button>
          </Tooltip>
          <Tooltip label={t('common.edit')}>
            <button
              onClick={onEdit}
              className="p-1.5 text-dark-500 hover:text-dark-300 transition rounded-lg hover:bg-dark-800/50"
            >
              <Pencil className="w-3.5 h-3.5" />
            </button>
          </Tooltip>
          <Tooltip label={t('common.delete')}>
            <button
              onClick={onDelete}
              className="p-1.5 text-dark-500 hover:text-red-400 transition rounded-lg hover:bg-dark-800/50"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </Tooltip>
        </div>
      </div>
    </motion.div>
  )
}
