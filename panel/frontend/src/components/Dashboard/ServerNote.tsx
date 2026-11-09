import { useRef, useState, type KeyboardEvent, type MouseEvent } from 'react'
import { Pencil } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { useServersStore } from '../../stores/serversStore'
import { Tooltip } from '../ui/Tooltip'

// Совпадает с длиной колонки servers.note
const NOTE_MAX_LENGTH = 40

interface Props {
  serverId: number
  note?: string | null
}

const stopCardClick = (e: MouseEvent) => e.stopPropagation()

/** Короткая подпись сервера рядом с IP: Enter или уход фокуса — сохранить, Esc — отменить */
export function ServerNote({ serverId, note }: Props) {
  const { t } = useTranslation()
  const updateServerNote = useServersStore(s => s.updateServerNote)
  const [draft, setDraft] = useState<string | null>(null)
  // Enter/Esc убирают поле, и браузер может следом прислать blur — второе закрытие игнорируем
  const closedRef = useRef(true)

  const startEditing = (e: MouseEvent) => {
    e.stopPropagation()
    closedRef.current = false
    setDraft(note ?? '')
  }

  const close = async (save: boolean) => {
    if (closedRef.current) return
    closedRef.current = true
    const next = (draft ?? '').trim() || null
    setDraft(null)
    if (!save || next === (note || null)) return
    try {
      await updateServerNote(serverId, next)
    } catch {
      toast.error(t('server_card.note_save_failed'))
    }
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      close(true)
    } else if (e.key === 'Escape') {
      e.preventDefault()
      close(false)
    }
  }

  if (draft !== null) {
    return (
      <input
        type="text"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={handleKeyDown}
        onBlur={() => close(true)}
        onClick={stopCardClick}
        maxLength={NOTE_MAX_LENGTH}
        placeholder={t('server_card.note_placeholder')}
        aria-label={t('server_card.note_placeholder')}
        className="input py-0.5 px-2 text-xs w-36"
        autoFocus
      />
    )
  }

  if (!note) {
    return (
      <Tooltip label={t('server_card.note_add')}>
        <button
          type="button"
          onClick={startEditing}
          aria-label={t('server_card.note_add')}
          className="p-0.5 rounded text-dark-500 hover:text-accent-400 transition-colors flex-shrink-0"
        >
          <Pencil className="w-3.5 h-3.5" />
        </button>
      </Tooltip>
    )
  }

  return (
    <Tooltip label={note}>
      <button
        type="button"
        onClick={startEditing}
        aria-label={t('server_card.note_edit')}
        className="flex items-center gap-1 min-w-0 px-1.5 py-0.5 rounded-md border border-dark-600/50 bg-dark-800/60
                   text-2xs text-dark-200 hover:border-accent-500/40 hover:text-accent-300 transition-colors"
      >
        <span className="truncate max-w-[9rem]">{note}</span>
        <Pencil className="w-3 h-3 flex-shrink-0 text-dark-500" />
      </button>
    </Tooltip>
  )
}
