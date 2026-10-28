import { useState, type KeyboardEvent } from 'react'
import { Folder, FolderPlus, FolderX, Plus } from 'lucide-react'
import { useTranslation } from 'react-i18next'

export interface FolderOption {
  name: string
  serverCount: number
}

interface Props {
  folders: FolderOption[]
  value: string | null
  onChange: (folder: string | null) => void
  disabled?: boolean
}

// Совпадает с длиной колонки servers.folder
const FOLDER_NAME_MAX_LENGTH = 200

const chipClass = (active: boolean) =>
  `flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-sm transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
    active
      ? 'border-accent-500/50 bg-accent-500/10 text-accent-300'
      : 'border-dark-700/50 bg-dark-800/50 text-dark-200 hover:text-dark-50'
  }`

/** Выбор папки плашками: существующие папки, «без папки» и новая по имени */
export function FolderChips({ folders, value, onChange, disabled = false }: Props) {
  const { t } = useTranslation()
  const [isCreating, setIsCreating] = useState(false)
  const [draft, setDraft] = useState('')

  // Новая папка появится в панели только вместе с сервером — до этого показываем её плашкой здесь
  const isNewFolder = value !== null && !folders.some(f => f.name === value)

  const finishCreating = () => {
    const name = draft.trim()
    if (name) onChange(name)
    setDraft('')
    setIsCreating(false)
  }

  const handleDraftKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    // Enter внутри формы иначе отправил бы её целиком
    if (e.key === 'Enter') {
      e.preventDefault()
      finishCreating()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      setDraft('')
      setIsCreating(false)
    }
  }

  return (
    <div className="flex flex-wrap gap-2">
      <button type="button" onClick={() => onChange(null)} disabled={disabled} className={chipClass(value === null)}>
        <FolderX className="w-4 h-4" />
        {t('dashboard.no_folder')}
      </button>

      {folders.map(folder => (
        <button
          key={folder.name}
          type="button"
          onClick={() => onChange(folder.name)}
          disabled={disabled}
          className={chipClass(value === folder.name)}
        >
          <Folder className="w-4 h-4" />
          <span className="max-w-[16rem] truncate">{folder.name}</span>
          <span className="text-xs text-dark-500">{folder.serverCount}</span>
        </button>
      ))}

      {isNewFolder && (
        <span className={chipClass(true)}>
          <FolderPlus className="w-4 h-4" />
          <span className="max-w-[16rem] truncate">{value}</span>
        </span>
      )}

      {isCreating ? (
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={handleDraftKeyDown}
          onBlur={finishCreating}
          placeholder={t('dashboard.folder_name_placeholder')}
          maxLength={FOLDER_NAME_MAX_LENGTH}
          className="input py-1.5 text-sm w-56"
          autoFocus
        />
      ) : (
        <button
          type="button"
          onClick={() => setIsCreating(true)}
          disabled={disabled}
          className={`${chipClass(false)} border-dashed`}
        >
          <Plus className="w-4 h-4" />
          {t('servers.folder_new')}
        </button>
      )}
    </div>
  )
}
