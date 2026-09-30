import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { ChevronDown, ChevronRight, Loader2, Settings2 } from 'lucide-react'
import { lossApi, type Server } from '../../api/client'
import { ServerSelector } from '../ssh/ServerSelector'

interface Props {
  servers: Server[]
  onSaved: () => void
}

function sameIds(a: number[], b: number[]): boolean {
  if (a.length !== b.length) return false
  const sortedB = [...b].sort((x, y) => x - y)
  return [...a].sort((x, y) => x - y).every((id, i) => id === sortedB[i])
}

export default function LossSettings({ servers, onSaved }: Props) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [saved, setSaved] = useState<number[] | null>(null)
  const [draft, setDraft] = useState<number[]>([])
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    lossApi.settings()
      .then(({ data }) => {
        setSaved(data.excluded_server_ids)
        setDraft(data.excluded_server_ids)
      })
      .catch(() => setSaved([]))
  }, [])

  const dirty = saved !== null && !sameIds(saved, draft)

  const save = async () => {
    setSaving(true)
    try {
      const { data } = await lossApi.updateSettings(draft)
      setSaved(data.excluded_server_ids)
      setDraft(data.excluded_server_ids)
      toast.success(t('loss.settings_saved'))
      onSaved()
    } catch {
      toast.error(t('loss.settings_save_failed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="bg-dark-900/50 rounded-xl border border-dark-800/50">
      <button onClick={() => setOpen(v => !v)} className="w-full flex items-center gap-2 px-4 py-3 text-left">
        {open ? <ChevronDown className="w-4 h-4 text-dark-500" /> : <ChevronRight className="w-4 h-4 text-dark-500" />}
        <Settings2 className="w-4 h-4 text-dark-400" />
        <span className="text-sm font-medium text-dark-200">{t('loss.settings_title')}</span>
        {saved !== null && saved.length > 0 && (
          <span className="text-xs text-dark-500">{t('loss.excluded_count', { count: saved.length })}</span>
        )}
      </button>
      {open && (
        <div className="border-t border-dark-800/50 p-4 space-y-3">
          <div>
            <h3 className="text-sm text-dark-200">{t('loss.excluded_title')}</h3>
            <p className="text-xs text-dark-500 mt-1">{t('loss.excluded_hint')}</p>
          </div>
          {saved === null ? (
            <Loader2 className="w-5 h-5 text-accent-400 animate-spin" />
          ) : (
            <>
              <ServerSelector servers={servers} selectedIds={draft} onChange={setDraft} />
              <div className="flex justify-end">
                <button
                  onClick={save}
                  disabled={!dirty || saving}
                  className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-40"
                >
                  {saving && <Loader2 className="w-4 h-4 animate-spin" />}
                  {t('common.save')}
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
