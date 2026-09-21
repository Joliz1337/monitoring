import { Loader2, Save, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Tooltip } from '../ui/Tooltip'
import type { WildcardReloadCmdPreset } from '../../api/client'

interface Props {
  presets: WildcardReloadCmdPreset[]
  value: string
  onPick: (command: string) => void
  onDelete: (name: string) => void
  onSaveCurrent?: () => void
  saving?: boolean
}

// Чипы сохранённых reload-команд: клик подставляет команду в поле,
// «Сохранить» превращает введённую команду в пресет
export default function ReloadCmdPresetChips({
  presets,
  value,
  onPick,
  onDelete,
  onSaveCurrent,
  saving = false,
}: Props) {
  const { t } = useTranslation()
  const trimmed = value.trim()
  const canSave = Boolean(onSaveCurrent) && trimmed.length > 0
    && !presets.some(p => p.command === trimmed)

  if (presets.length === 0 && !canSave) return null

  return (
    <div className="flex flex-wrap gap-2">
      {presets.map(p => {
        const active = p.command === trimmed
        return (
          <div
            key={p.name}
            className={`flex items-center rounded-lg border text-xs overflow-hidden ${
              active
                ? 'border-accent-500/50 bg-accent-500/10'
                : 'border-dark-700/50 bg-dark-800/50'
            }`}
          >
            <Tooltip label={p.command}>
              <button
                type="button"
                onClick={() => onPick(p.command)}
                className={`px-2.5 py-1.5 transition-colors ${
                  active ? 'text-accent-300' : 'text-dark-200 hover:text-dark-50'
                }`}
              >
                {p.name}
              </button>
            </Tooltip>
            <Tooltip label={t('common.delete')}>
              <button
                type="button"
                onClick={() => onDelete(p.name)}
                className="px-1.5 py-1.5 text-dark-500 hover:text-danger hover:bg-danger/10 transition-colors"
              >
                <X className="w-3 h-3" />
              </button>
            </Tooltip>
          </div>
        )
      })}
      {canSave && (
        <button
          type="button"
          onClick={onSaveCurrent}
          disabled={saving}
          className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-dark-700/50 bg-dark-800/50 text-xs text-dark-200 hover:text-dark-50 transition-colors disabled:opacity-50"
        >
          {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Save className="w-3 h-3" />}
          {t('wildcard_ssl.reload_preset_save')}
        </button>
      )}
    </div>
  )
}
