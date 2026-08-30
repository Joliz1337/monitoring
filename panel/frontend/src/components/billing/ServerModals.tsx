import { useState } from 'react'
import { Clock, Loader2, Wallet, X } from 'lucide-react'
import { toast } from 'sonner'
import { billingApi, BillingServerData } from '../../api/client'
import { CloudProviderId, PROVIDER_IDS, PROVIDERS, getProvider } from './providers'
import {
  Field, INPUT_CLASS, Overlay, PaidTotalHint, QUICK_DAYS, Translate,
  currencySymbol, formatDays, statusColor, useBillingDateFormat,
} from './shared'

type BillingType = 'monthly' | 'resource' | 'cloud'

interface FormValues {
  name: string
  billingType: BillingType
  provider: CloudProviderId
  paidMode: 'days' | 'date'
  paidDays: number
  paidUntil: string
  dailyCost: string
  balance: string
  currency: string
  notes: string
  folder: string
  threshold: string
  credentials: Record<string, string>
}

const CURRENCIES = ['RUB', 'USD', 'EUR'] as const

function emptyValues(): FormValues {
  return {
    name: '',
    billingType: 'resource',
    provider: 'yandex_cloud',
    paidMode: 'days',
    paidDays: 30,
    paidUntil: '',
    dailyCost: '',
    balance: '',
    currency: 'RUB',
    notes: '',
    folder: '',
    threshold: '0',
    credentials: {},
  }
}

function valuesFromServer(server: BillingServerData): FormValues {
  const currentDaily = server.monthly_cost ? server.monthly_cost / 30 : 0
  return {
    name: server.name,
    billingType: server.billing_type as BillingType,
    provider: (server.cloud_provider as CloudProviderId) || 'yandex_cloud',
    paidMode: 'date',
    paidDays: 30,
    paidUntil: server.paid_until
      ? new Date(server.paid_until).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' })
      : '',
    dailyCost: currentDaily ? currentDaily.toFixed(2) : '',
    balance: server.account_balance?.toString() || '',
    currency: server.currency,
    notes: server.notes || '',
    folder: server.folder || '',
    threshold: server.cloud_balance_threshold?.toString() || '0',
    credentials: { cloud_account_id: server.cloud_account_id || '' },
  }
}

function ModalHeader({ title, onClose }: { title: string; onClose: () => void }) {
  return (
    <div className="flex items-center justify-between mb-5">
      <h2 className="text-lg font-semibold text-white">{title}</h2>
      <button onClick={onClose} className="text-dark-500 hover:text-dark-300 transition">
        <X className="w-5 h-5" />
      </button>
    </div>
  )
}

function CredentialFields({ values, setValues, t, server }: {
  values: FormValues
  setValues: (patch: Partial<FormValues>) => void
  t: Translate
  server?: BillingServerData
}) {
  const provider = PROVIDERS[values.provider]

  return (
    <>
      {provider.fields.map(field => {
        const stored = field.key === 'cloud_credential' && server?.has_cloud_credential
        return (
          <Field
            key={field.key}
            label={
              field.secret && stored
                ? `${t(field.labelKey)} (${t('billing.cloud_token_change_hint')})`
                : t(field.labelKey)
            }
          >
            <input
              type={field.secret ? 'password' : 'text'}
              value={values.credentials[field.key] || ''}
              onChange={e => setValues({
                credentials: { ...values.credentials, [field.key]: e.target.value },
              })}
              placeholder={field.secret && stored ? '••••••••' : field.placeholder}
              className={INPUT_CLASS}
            />
            <p className="text-[10px] text-dark-500 mt-1">
              {t(field.hintKey)}{' '}
              {field.link && (
                <a href={field.link.url} target="_blank" rel="noopener noreferrer"
                   className="text-accent-400 hover:text-accent-300 underline transition">
                  {field.link.labelKey ? t(field.link.labelKey) : field.link.label}
                </a>
              )}
            </p>
          </Field>
        )
      })}
      <Field label={t('billing.cloud_threshold')}>
        <input
          type="number"
          step="0.01"
          value={values.threshold}
          onChange={e => setValues({ threshold: e.target.value })}
          placeholder="0"
          className={INPUT_CLASS}
        />
        <p className="text-[10px] text-dark-500 mt-1">{t('billing.cloud_threshold_hint')}</p>
      </Field>
    </>
  )
}

function ServerForm({ values, setValues, t, folders, mode, server }: {
  values: FormValues
  setValues: (patch: Partial<FormValues>) => void
  t: Translate
  folders: string[]
  mode: 'add' | 'edit'
  server?: BillingServerData
}) {
  const { formatDateTime } = useBillingDateFormat()

  return (
    <div className="space-y-4">
      {mode === 'add' && (
        <Field label={t('billing.billing_type')}>
          <div className="flex gap-2">
            {(['monthly', 'resource', 'cloud'] as const).map(bt => (
              <button
                key={bt}
                onClick={() => setValues({
                  billingType: bt,
                  currency: bt === 'cloud' ? PROVIDERS[values.provider].defaultCurrency : values.currency,
                })}
                className={`flex-1 py-2.5 rounded-lg text-sm font-medium transition ${
                  values.billingType === bt
                    ? 'bg-accent-500/20 text-accent-400 border border-accent-500/30'
                    : 'bg-dark-800 text-dark-400 border border-dark-700/50'
                }`}
              >
                <div>{t(`billing.type_${bt}`)}</div>
                <div className={`text-[10px] mt-0.5 ${values.billingType === bt ? 'text-accent-400/60' : 'text-dark-500'}`}>
                  {t(`billing.type_${bt}_hint`)}
                </div>
              </button>
            ))}
          </div>
        </Field>
      )}

      <Field label={t('common.name')}>
        <input
          value={values.name}
          onChange={e => setValues({ name: e.target.value })}
          placeholder={t('billing.name_placeholder')}
          className={INPUT_CLASS}
          autoFocus
        />
      </Field>

      {values.billingType === 'cloud' && mode === 'add' && (
        <Field label={t('billing.provider')}>
          <div className="flex gap-2">
            {PROVIDER_IDS.map(id => (
              <button
                key={id}
                onClick={() => setValues({
                  provider: id,
                  currency: PROVIDERS[id].defaultCurrency,
                  credentials: {},
                })}
                className={`flex-1 py-2 rounded-lg text-sm font-medium transition ${
                  values.provider === id
                    ? PROVIDERS[id].accent.quickActive
                    : 'bg-dark-800 text-dark-400 border border-dark-700/50'
                }`}
              >
                {t(PROVIDERS[id].nameKey)}
              </button>
            ))}
          </div>
        </Field>
      )}

      {values.billingType === 'monthly' && (
        <Field label={t('billing.paid_until')} faqScreen="BILLING_QUOTA">
          {mode === 'add' && (
            <div className="flex gap-2 mb-2">
              {(['days', 'date'] as const).map(m => (
                <button
                  key={m}
                  onClick={() => setValues({ paidMode: m })}
                  className={`flex-1 py-1.5 rounded-lg text-xs font-medium transition ${
                    values.paidMode === m
                      ? 'bg-accent-500/20 text-accent-400 border border-accent-500/30'
                      : 'bg-dark-800 text-dark-400 border border-dark-700/50'
                  }`}
                >
                  {t(`billing.mode_${m}`)}
                </button>
              ))}
            </div>
          )}
          {mode === 'add' && values.paidMode === 'days' ? (
            <input
              type="number"
              value={values.paidDays}
              onChange={e => setValues({ paidDays: parseInt(e.target.value) || 0 })}
              min={1}
              className={INPUT_CLASS}
            />
          ) : (
            <>
              <input
                type="text"
                value={values.paidUntil}
                onChange={e => setValues({ paidUntil: e.target.value })}
                placeholder={t('billing.paid_until_placeholder')}
                className={INPUT_CLASS}
              />
              <p className="text-xs text-dark-500 mt-1">{t('billing.paid_until_hint')}</p>
            </>
          )}
        </Field>
      )}

      {values.billingType === 'resource' && (
        <>
          <Field label={t('billing.daily_cost')} faqScreen="BILLING_QUOTA">
            <input
              type="number"
              step="0.01"
              value={values.dailyCost}
              onChange={e => setValues({ dailyCost: e.target.value })}
              placeholder="0.00"
              className={INPUT_CLASS}
            />
          </Field>
          <Field label={t('billing.account_balance')}>
            <input
              type="number"
              step="0.01"
              value={values.balance}
              onChange={e => setValues({ balance: e.target.value })}
              placeholder="0.00"
              className={INPUT_CLASS}
            />
          </Field>
        </>
      )}

      {values.billingType === 'cloud' && (
        <>
          <CredentialFields values={values} setValues={setValues} t={t} server={server} />
          {server?.cloud_last_sync_at && (
            <div className="text-xs text-dark-500">
              {t('billing.cloud_last_sync')}: {formatDateTime(server.cloud_last_sync_at)}
            </div>
          )}
          {server?.cloud_last_error && (
            <div className="text-xs text-red-400 bg-red-500/10 rounded-lg px-3 py-2">
              {server.cloud_last_error}
            </div>
          )}
        </>
      )}

      <Field label={t('billing.currency')}>
        <div className="flex gap-2">
          {CURRENCIES.map(c => (
            <button
              key={c}
              onClick={() => setValues({ currency: c })}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                values.currency === c
                  ? 'bg-accent-500/20 text-accent-400 border border-accent-500/30'
                  : 'bg-dark-800 text-dark-400 border border-dark-700/50'
              }`}
            >
              {currencySymbol(c)} {c}
            </button>
          ))}
        </div>
      </Field>

      <Field label={t('billing.notes') + ` (${t('common.optional')})`}>
        <input
          value={values.notes}
          onChange={e => setValues({ notes: e.target.value })}
          placeholder={t('billing.notes_placeholder')}
          className={INPUT_CLASS}
        />
      </Field>

      {(folders.length > 0 || mode === 'edit') && (
        <Field label={t('billing.folder') + ` (${t('common.optional')})`}>
          <select
            value={values.folder}
            onChange={e => setValues({ folder: e.target.value })}
            className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                       focus:border-accent-500/50 focus:outline-none transition"
          >
            <option value="">{t('billing.no_folder')}</option>
            {folders.map(f => <option key={f} value={f}>{f}</option>)}
          </select>
        </Field>
      )}
    </div>
  )
}

export function AddModal({ t, folders, onClose, onCreated }: {
  t: Translate
  folders: string[]
  onClose: () => void
  onCreated: (s: BillingServerData) => void
}) {
  const [values, setAll] = useState<FormValues>(emptyValues)
  const [saving, setSaving] = useState(false)
  const setValues = (patch: Partial<FormValues>) => setAll(prev => ({ ...prev, ...patch }))

  const isCloud = values.billingType === 'cloud'

  const submit = async () => {
    if (!values.name.trim()) return
    setSaving(true)
    try {
      const dailyNum = parseFloat(values.dailyCost) || 0
      const res = await billingApi.createServer({
        name: values.name.trim(),
        billing_type: values.billingType,
        paid_days: values.billingType === 'monthly' && values.paidMode === 'days' ? values.paidDays : undefined,
        paid_until: values.billingType === 'monthly' && values.paidMode === 'date' ? values.paidUntil : undefined,
        monthly_cost: values.billingType === 'resource' ? dailyNum * 30 : undefined,
        account_balance: values.billingType === 'resource' ? parseFloat(values.balance) || 0 : undefined,
        currency: values.currency,
        notes: values.notes.trim() || undefined,
        folder: values.folder || undefined,
        cloud_provider: isCloud ? values.provider : undefined,
        cloud_credential: isCloud ? values.credentials.cloud_credential : undefined,
        cloud_account_id: isCloud ? values.credentials.cloud_account_id : undefined,
        cloud_balance_threshold: isCloud ? parseFloat(values.threshold) || 0 : undefined,
      })
      onCreated(res.data.server)
      toast.success(t('common.added'))
    } catch {
      toast.error(t('common.action_failed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Overlay onClose={onClose}>
      <div className="p-6">
        <ModalHeader title={t('billing.add')} onClose={onClose} />
        <ServerForm values={values} setValues={setValues} t={t} folders={folders} mode="add" />
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">
            {t('common.cancel')}
          </button>
          <button
            onClick={submit}
            disabled={!values.name.trim() || saving}
            className="flex-1 py-2.5 bg-accent-500 text-white rounded-xl text-sm font-medium hover:bg-accent-600 transition
                       disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
          >
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('common.add')}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

export function EditModal({ t, server, folders, onClose, onSaved }: {
  t: Translate
  server: BillingServerData
  folders: string[]
  onClose: () => void
  onSaved: (s: BillingServerData) => void
}) {
  const [values, setAll] = useState<FormValues>(() => valuesFromServer(server))
  const [saving, setSaving] = useState(false)
  const setValues = (patch: Partial<FormValues>) => setAll(prev => ({ ...prev, ...patch }))

  const submit = async () => {
    setSaving(true)
    try {
      const payload: Record<string, unknown> = {
        name: values.name,
        currency: values.currency,
        notes: values.notes || null,
        folder: values.folder || null,
      }
      if (server.billing_type === 'monthly') {
        payload.paid_until = values.paidUntil || null
      }
      if (server.billing_type === 'resource') {
        payload.monthly_cost = (parseFloat(values.dailyCost) || 0) * 30
        payload.account_balance = parseFloat(values.balance) || 0
      }
      if (server.billing_type === 'cloud') {
        payload.cloud_account_id = values.credentials.cloud_account_id || null
        if (values.credentials.cloud_credential) {
          payload.cloud_credential = values.credentials.cloud_credential
        }
        payload.cloud_balance_threshold = parseFloat(values.threshold) || 0
      }
      const res = await billingApi.updateServer(server.id, payload as never)
      onSaved(res.data)
      toast.success(t('common.saved'))
    } catch {
      toast.error(t('common.action_failed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Overlay onClose={onClose}>
      <div className="p-6">
        <ModalHeader title={t('common.edit')} onClose={onClose} />
        <ServerForm
          values={values}
          setValues={setValues}
          t={t}
          folders={folders}
          mode="edit"
          server={server}
        />
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">
            {t('common.cancel')}
          </button>
          <button
            onClick={submit}
            disabled={saving}
            className="flex-1 py-2.5 bg-accent-500 text-white rounded-xl text-sm font-medium hover:bg-accent-600 transition
                       disabled:opacity-40 flex items-center justify-center gap-2"
          >
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('common.save')}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

export function ExtendModal({ t, server, onClose, onDone }: {
  t: Translate
  server: BillingServerData
  onClose: () => void
  onDone: (s: BillingServerData) => void
}) {
  const [days, setDays] = useState(30)
  const [saving, setSaving] = useState(false)
  const { formatDateTime } = useBillingDateFormat()

  // Бэкенд продлевает от текущей даты окончания, а если срок уже истёк — от «сейчас»
  const totalDays = Math.max(server.days_left ?? 0, 0) + days

  const submit = async () => {
    if (days <= 0) return
    setSaving(true)
    try {
      const res = await billingApi.extendServer(server.id, days)
      onDone(res.data)
      toast.success(t('billing.extended'))
    } catch {
      toast.error(t('common.action_failed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Overlay onClose={onClose}>
      <div className="p-6">
        <ModalHeader title={`${t('billing.extend')} — ${server.name}`} onClose={onClose} />

        <div className="space-y-4">
          <Field label={t('billing.extend_days')}>
            <input
              type="number"
              value={days}
              onChange={e => setDays(parseInt(e.target.value) || 0)}
              min={1}
              className={INPUT_CLASS}
              autoFocus
            />
          </Field>
          <div className="flex flex-wrap gap-2">
            {QUICK_DAYS.map(d => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                  days === d
                    ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                    : 'bg-dark-800 text-dark-400 border border-dark-700/50'
                }`}
              >
                +{d}d
              </button>
            ))}
          </div>
          {days > 0 && (
            <div className="text-xs text-emerald-400/80 bg-emerald-500/10 rounded-lg px-3 py-2">
              <PaidTotalHint totalDays={totalDays} t={t} formatDateTime={formatDateTime} />
            </div>
          )}
        </div>

        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">
            {t('common.cancel')}
          </button>
          <button
            onClick={submit}
            disabled={days <= 0 || saving}
            className="flex-1 py-2.5 bg-emerald-500 text-white rounded-xl text-sm font-medium hover:bg-emerald-600 transition
                       disabled:opacity-40 flex items-center justify-center gap-2"
          >
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('billing.extend')}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

export function TopupModal({ t, server, onClose, onDone }: {
  t: Translate
  server: BillingServerData
  onClose: () => void
  onDone: (s: BillingServerData) => void
}) {
  const [amount, setAmount] = useState('')
  const [saving, setSaving] = useState(false)
  const { formatDateTime } = useBillingDateFormat()

  const numAmount = parseFloat(amount) || 0
  const monthlyCost = server.monthly_cost ?? 0
  const addedDays = monthlyCost > 0 ? (numAmount / monthlyCost) * 30 : 0
  // Как на бэкенде: срок считается от суммы текущего (уже «прожитого») баланса и пополнения
  const totalDays = monthlyCost > 0 ? (((server.account_balance ?? 0) + numAmount) / monthlyCost) * 30 : 0

  const submit = async () => {
    if (numAmount <= 0) return
    setSaving(true)
    try {
      const res = await billingApi.topupServer(server.id, numAmount)
      onDone(res.data)
      toast.success(t('billing.topped_up'))
    } catch {
      toast.error(t('common.action_failed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Overlay onClose={onClose}>
      <div className="p-6">
        <ModalHeader title={`${t('billing.topup')} — ${server.name}`} onClose={onClose} />

        <div className="space-y-4">
          <div className="bg-dark-800/50 rounded-xl p-3 border border-dark-700/50">
            <div className="flex items-center justify-between">
              <span className="text-xs text-dark-500">{t('billing.current_balance')}</span>
              <span className="text-lg font-bold text-dark-200">
                {(server.account_balance ?? 0).toFixed(2)} {currencySymbol(server.currency)}
              </span>
            </div>
            {monthlyCost > 0 && (
              <div className="flex items-center justify-between mt-1">
                <span className="text-xs text-dark-500">{t('billing.daily_cost')}</span>
                <span className="text-xs text-dark-400">
                  {(monthlyCost / 30).toFixed(2)} {currencySymbol(server.currency)}{t('billing.per_day')}
                </span>
              </div>
            )}
          </div>
          <Field label={t('billing.topup_amount') + ` (${currencySymbol(server.currency)})`}>
            <input
              type="number"
              step="0.01"
              value={amount}
              onChange={e => setAmount(e.target.value)}
              placeholder="0.00"
              className={INPUT_CLASS}
              autoFocus
            />
          </Field>
          <div className="flex flex-wrap gap-2">
            {[100, 500, 1000, 2000, 5000].map(v => (
              <button
                key={v}
                onClick={() => setAmount(prev => ((parseFloat(prev) || 0) + v).toString())}
                className="px-3 py-1.5 rounded-lg text-xs font-medium transition
                  bg-dark-800 text-dark-400 border border-dark-700/50 hover:border-dark-600
                  hover:bg-purple-500/10 hover:text-purple-400 hover:border-purple-500/30
                  active:scale-95"
              >
                +{v} {currencySymbol(server.currency)}
              </button>
            ))}
          </div>
          {numAmount > 0 && monthlyCost > 0 && (
            <div className="text-xs text-emerald-400/80 bg-emerald-500/10 rounded-lg px-3 py-2 space-y-1">
              <div className="flex items-center gap-1">
                <Clock className="w-3 h-3" />
                ≈ +{Math.round(addedDays)} {t('common.days')}
              </div>
              <PaidTotalHint totalDays={totalDays} t={t} formatDateTime={formatDateTime} />
            </div>
          )}
        </div>

        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">
            {t('common.cancel')}
          </button>
          <button
            onClick={submit}
            disabled={numAmount <= 0 || saving}
            className="flex-1 py-2.5 bg-purple-500 text-white rounded-xl text-sm font-medium hover:bg-purple-600 transition
                       disabled:opacity-40 flex items-center justify-center gap-2"
          >
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('billing.topup')}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

/** Расчёт пополнения облачного баланса: облако пополняется у провайдера, здесь только арифметика */
export function CloudPlanModal({ t, server, onClose }: {
  t: Translate
  server: BillingServerData
  onClose: () => void
}) {
  const { formatDateTime } = useBillingDateFormat()
  const [plan, setPlan] = useState<{ by: 'days' | 'amount'; value: string }>({ by: 'days', value: '30' })

  const provider = getProvider(server.cloud_provider)
  const accent = provider?.accent ?? PROVIDERS.yandex_cloud.accent
  const currency = currencySymbol(server.currency)
  const dailyCost = server.cloud_daily_cost ?? 0
  const canPlan = dailyCost > 0
  // Как compute_days_left на бэкенде: тратить можно только то, что выше порога
  const usable = (server.account_balance ?? 0) - (server.cloud_balance_threshold ?? 0)
  const entered = parseFloat(plan.value) || 0
  const targetDays = !canPlan ? 0 : plan.by === 'days' ? entered : Math.max(0, (usable + entered) / dailyCost)
  const requiredAmount = plan.by === 'days' ? Math.max(0, targetDays * dailyCost - usable) : entered
  const lastsDays = requiredAmount > 0 || !canPlan ? targetDays : usable / dailyCost
  const daysField = plan.by === 'days' ? plan.value : targetDays.toFixed(1)
  const amountField = plan.by === 'amount' ? plan.value : requiredAmount.toFixed(2)

  return (
    <Overlay onClose={onClose}>
      <div className="p-6">
        <ModalHeader title={`${t('billing.plan_title')} — ${server.name}`} onClose={onClose} />

        <div className="space-y-4">
          <div className="bg-dark-800/50 rounded-xl p-3 border border-dark-700/50 space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-xs text-dark-500">{t('billing.current_balance')}</span>
              <span className="text-lg font-bold text-dark-200">
                {(server.account_balance ?? 0).toFixed(2)} {currency}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-dark-500">{t('billing.cloud_threshold')}</span>
              <span className="text-xs text-dark-400">{(server.cloud_balance_threshold ?? 0).toFixed(2)} {currency}</span>
            </div>
            {canPlan && (
              <div className="flex items-center justify-between">
                <span className="text-xs text-dark-500">{t('billing.daily_cost')}</span>
                <span className="text-xs text-dark-400">{dailyCost.toFixed(2)} {currency}{t('billing.per_day')}</span>
              </div>
            )}
            {server.days_left !== null && (
              <div className="flex items-center justify-between">
                <span className="text-xs text-dark-500">{t('billing.plan_days_left')}</span>
                <span className={`text-xs font-medium ${statusColor(server.days_left)}`}>
                  {formatDays(server.days_left, t)}
                </span>
              </div>
            )}
          </div>

          {!canPlan ? (
            <div className="text-xs text-yellow-400/80 bg-yellow-500/10 rounded-lg px-3 py-2">
              {t('billing.plan_no_cost')}
            </div>
          ) : (
            <>
              <Field label={t('billing.plan_days')}>
                <input
                  type="number"
                  min={1}
                  step="1"
                  value={daysField}
                  onChange={e => setPlan({ by: 'days', value: e.target.value })}
                  className={INPUT_CLASS}
                  autoFocus
                />
              </Field>
              <div className="flex flex-wrap gap-2">
                {QUICK_DAYS.map(d => (
                  <button
                    key={d}
                    onClick={() => setPlan({ by: 'days', value: String(d) })}
                    className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                      plan.by === 'days' && entered === d
                        ? accent.quickActive
                        : 'bg-dark-800 text-dark-400 border border-dark-700/50 hover:border-dark-600'
                    }`}
                  >
                    {d}{t('billing.short_days')}
                  </button>
                ))}
              </div>
              <Field label={`${t('billing.plan_amount')} (${currency})`}>
                <input
                  type="number"
                  min={0}
                  step="0.01"
                  value={amountField}
                  onChange={e => setPlan({ by: 'amount', value: e.target.value })}
                  className={INPUT_CLASS}
                />
              </Field>
              {entered > 0 && (
                <div className={`text-xs rounded-lg px-3 py-2 space-y-1 ${accent.hintBox}`}>
                  {requiredAmount > 0 ? (
                    <div className="flex items-center gap-1">
                      <Wallet className="w-3 h-3" />
                      {t('billing.plan_topup')}: <span className="font-semibold">{requiredAmount.toFixed(2)} {currency}</span>
                    </div>
                  ) : (
                    <div>{t('billing.plan_enough')}</div>
                  )}
                  <PaidTotalHint totalDays={lastsDays} labelKey="billing.plan_lasts" t={t} formatDateTime={formatDateTime} />
                </div>
              )}
              <p className="text-[11px] text-dark-500">{t('billing.plan_hint')}</p>
            </>
          )}
        </div>

        <div className="mt-6">
          <button onClick={onClose} className="w-full py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">
            {t('common.close')}
          </button>
        </div>
      </div>
    </Overlay>
  )
}
