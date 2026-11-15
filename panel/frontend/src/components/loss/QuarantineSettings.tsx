import { useTranslation } from 'react-i18next'
import type { QuarantineMode, QuarantineSettingsData, Server } from '../../api/client'
import { Toggle } from '../ui/Toggle'
import { ServerSelector } from '../ssh/ServerSelector'
import AddressList from './AddressList'

interface Props {
  servers: Server[]
  value: QuarantineSettingsData
  onChange: (next: QuarantineSettingsData) => void
}

const MODES: QuarantineMode[] = ['off', 'observe', 'enforce']
const SECONDS_IN_MINUTE = 60

type ThresholdKey =
  | 'loss_threshold'
  | 'sustained_seconds'
  | 'downs_threshold'
  | 'calm_seconds'
  | 'chronic_strikes'
  | 'max_share_pct'

interface Threshold {
  key: ThresholdKey
  min: number
  max: number
  // Удержание и затишье хранятся в секундах, а вводятся в минутах
  perInputUnit: number
  wholeOnly: boolean
}

// Границы те же, что проверяет панель при сохранении
const THRESHOLDS: Threshold[] = [
  { key: 'loss_threshold', min: 50, max: 90, perInputUnit: 1, wholeOnly: false },
  { key: 'sustained_seconds', min: 60, max: 1800, perInputUnit: SECONDS_IN_MINUTE, wholeOnly: true },
  { key: 'downs_threshold', min: 2, max: 20, perInputUnit: 1, wholeOnly: true },
  { key: 'calm_seconds', min: 120, max: 3600, perInputUnit: SECONDS_IN_MINUTE, wholeOnly: true },
  { key: 'chronic_strikes', min: 2, max: 20, perInputUnit: 1, wholeOnly: true },
  { key: 'max_share_pct', min: 10, max: 90, perInputUnit: 1, wholeOnly: true },
]

// Пустое поле хранится как NaN и границы не проходит
function isWithinBounds(threshold: Threshold, stored: number): boolean {
  if (threshold.wholeOnly && !Number.isInteger(stored)) return false
  return stored >= threshold.min && stored <= threshold.max
}

export function isQuarantineValid(settings: QuarantineSettingsData): boolean {
  return THRESHOLDS.every(threshold => isWithinBounds(threshold, settings[threshold.key]))
}

function fromInput(threshold: Threshold, typed: string): number {
  if (typed === '') return NaN
  const stored = Number(typed) * threshold.perInputUnit
  return threshold.perInputUnit === 1 ? stored : Math.round(stored)
}

export default function QuarantineSettings({ servers, value, onChange }: Props) {
  const { t } = useTranslation()

  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-sm font-medium text-dark-100">{t('loss.quarantine_settings_title')}</h3>
        <p className="text-xs text-dark-500 mt-1">{t('loss.quarantine_settings_hint')}</p>
      </div>

      <div className="flex gap-2 flex-wrap">
        {MODES.map(mode => (
          <button
            key={mode}
            type="button"
            onClick={() => onChange({ ...value, mode })}
            className={`px-3 py-1.5 rounded-lg text-sm border transition-colors ${
              value.mode === mode
                ? 'bg-accent-500/15 text-accent-300 border-accent-500/40'
                : 'text-dark-400 border-dark-700 hover:text-dark-200'
            }`}
          >
            {t(`loss.quarantine_mode_${mode}`)}
          </button>
        ))}
      </div>

      <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {THRESHOLDS.map(threshold => {
          const stored = value[threshold.key]
          const valid = isWithinBounds(threshold, stored)
          return (
            <div key={threshold.key}>
              <label className="block text-xs text-dark-400 mb-1">{t(`loss.quarantine_${threshold.key}`)}</label>
              <input
                type="number"
                min={threshold.min / threshold.perInputUnit}
                max={threshold.max / threshold.perInputUnit}
                value={Number.isNaN(stored) ? '' : stored / threshold.perInputUnit}
                onChange={e => onChange({ ...value, [threshold.key]: fromInput(threshold, e.target.value) })}
                className={`input text-sm ${valid ? '' : 'border-danger/60'}`}
              />
              <p className="text-xs text-dark-500 mt-1">
                {t(`loss.quarantine_${threshold.key}_hint`)}{' '}
                <span className={`whitespace-nowrap ${valid ? '' : 'text-danger'}`}>
                  {t('loss.quarantine_field_range', {
                    min: threshold.min / threshold.perInputUnit,
                    max: threshold.max / threshold.perInputUnit,
                  })}
                </span>
              </p>
            </div>
          )
        })}
      </div>

      <div className="flex items-center justify-between gap-4">
        <div>
          <p className="text-sm text-dark-200">{t('loss.quarantine_drop_sessions')}</p>
          <p className="text-xs text-dark-500">{t('loss.quarantine_drop_sessions_hint')}</p>
        </div>
        <Toggle on={value.drop_sessions} onClick={() => onChange({ ...value, drop_sessions: !value.drop_sessions })} />
      </div>
      <div className="flex items-center justify-between gap-4">
        <div>
          <p className="text-sm text-dark-200">{t('loss.quarantine_notify')}</p>
          <p className="text-xs text-dark-500">{t('loss.quarantine_notify_hint')}</p>
        </div>
        <Toggle on={value.notify} onClick={() => onChange({ ...value, notify: !value.notify })} />
      </div>

      <div className="space-y-3">
        <div>
          <h3 className="text-sm text-dark-200">{t('loss.quarantine_exempt_servers_title')}</h3>
          <p className="text-xs text-dark-500 mt-1">{t('loss.quarantine_exempt_servers_hint')}</p>
        </div>
        <ServerSelector
          servers={servers}
          selectedIds={value.exempt_server_ids}
          onChange={ids => onChange({ ...value, exempt_server_ids: ids })}
        />
      </div>
      <div className="space-y-3">
        <div>
          <h3 className="text-sm text-dark-200">{t('loss.quarantine_exempt_targets_title')}</h3>
          <p className="text-xs text-dark-500 mt-1">{t('loss.quarantine_exempt_targets_hint')}</p>
        </div>
        <AddressList
          addresses={value.exempt_targets}
          onAdd={added => onChange({ ...value, exempt_targets: [...value.exempt_targets, ...added] })}
          onRemove={removed => onChange({ ...value, exempt_targets: value.exempt_targets.filter(address => address !== removed) })}
        />
      </div>
    </div>
  )
}
