import { memo, useCallback, useEffect, useMemo, useState } from 'react'
import { motion } from 'framer-motion'
import { KeyRound, Loader2, Save, ShieldCheck, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  sshAccessApi,
  type SshAccessOverview as Overview,
  type SshAccessPart,
  type SshAccessScope,
  type SshAccessTarget,
  type SshCheckState,
} from '../../api/client'
import { orderFolders } from '../../utils/folders'
import { ServerSelector } from '../ssh/ServerSelector'
import SshCredsFields from '../servers/SshCredsFields'
import { Checkbox } from '../ui/Checkbox'
import {
  ACCESS_FORM_BLANK, accessFormErrors, isAccessFormDirty, toAccessChange, type AccessFormValue,
} from './accessForm'
import SshAccessCheck from './SshAccessCheck'
import SshAccessOverview, { ALL_ACCESS_PARTS } from './SshAccessOverview'
import SshProxyFields from './SshProxyFields'

// Пока проверка идёт, её ход перечитывается с этим шагом
const CHECK_POLL_INTERVAL_MS = 2000
const SCOPES: SshAccessScope[] = ['global', 'folders', 'servers']

export interface SshAccessNode {
  id: number
  name: string
  url: string
  folder: string | null
}

interface Props {
  nodes: SshAccessNode[]
  /** Окно открывается на «Серверам» с этим выбором */
  initialServerIds?: number[]
  /** Доступ изменён — странице пора перечитать, у кого он есть */
  onChanged: () => void
  onClose: () => void
}

function SshAccessModal({ nodes, initialServerIds, onChanged, onClose }: Props) {
  const { t } = useTranslation()

  const [overview, setOverview] = useState<Overview | null>(null)
  const [check, setCheck] = useState<SshCheckState | null>(null)
  const [scope, setScope] = useState<SshAccessScope>(initialServerIds?.length ? 'servers' : 'global')
  const [folders, setFolders] = useState<string[]>([])
  const [serverIds, setServerIds] = useState<number[]>(initialServerIds ?? [])
  const [form, setForm] = useState<AccessFormValue>(ACCESS_FORM_BLANK)
  const [busy, setBusy] = useState(false)

  const failureText = useCallback(
    // Отказ проверки тела запроса приходит списком полей, а не текстом — в уведомление идёт только строка
    (err: any) => {
      const detail = err?.response?.data?.detail
      return typeof detail === 'string' && detail ? detail : t('sshAccess.failed')
    },
    [t],
  )

  const loadOverview = useCallback(async () => {
    try {
      setOverview((await sshAccessApi.overview()).data)
    } catch (err) {
      toast.error(failureText(err))
    }
  }, [failureText])

  const loadCheck = useCallback(async () => {
    try {
      setCheck((await sshAccessApi.checkState()).data.check)
    } catch {
      // Ход проверки перечитается следующим опросом; сама она идёт на бэкенде
    }
  }, [])

  useEffect(() => {
    loadOverview()
    loadCheck()
  }, [loadOverview, loadCheck])

  const checkRunning = check?.status === 'running'
  useEffect(() => {
    if (!checkRunning) return
    const timer = setInterval(loadCheck, CHECK_POLL_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [checkRunning, loadCheck])

  const folderOptions = useMemo(() => {
    const sizes = new Map<string, number>()
    for (const node of nodes) {
      if (node.folder) sizes.set(node.folder, (sizes.get(node.folder) ?? 0) + 1)
    }
    return orderFolders([...sizes.keys()]).map(name => ({ name, size: sizes.get(name)! }))
  }, [nodes])

  const selectorServers = useMemo(
    () => nodes.map(node => ({ ...node, is_active: true })),
    [nodes],
  )

  const target: SshAccessTarget = scope === 'folders'
    ? { scope, folders }
    : scope === 'servers' ? { scope, server_ids: serverIds } : { scope }
  const targetChosen = scope === 'global' || (scope === 'folders' ? folders.length > 0 : serverIds.length > 0)
  const dirty = isAccessFormDirty(form)
  const errors = accessFormErrors(form)
  const formInvalid = errors.port || errors.proxy

  const toggleFolder = (name: string) => {
    setFolders(prev => (prev.includes(name) ? prev.filter(f => f !== name) : [...prev, name]))
  }

  const changeAccess = async (request: () => Promise<unknown>, doneText: string) => {
    setBusy(true)
    try {
      await request()
      toast.success(doneText)
      await loadOverview()
      onChanged()
      return true
    } catch (err) {
      toast.error(failureText(err))
      return false
    } finally {
      setBusy(false)
    }
  }

  const handleSave = async () => {
    const saved = await changeAccess(() => sshAccessApi.save(target, toAccessChange(form)), t('sshAccess.saved'))
    if (saved) setForm(ACCESS_FORM_BLANK)
  }

  const handleClear = (clearTarget: SshAccessTarget, parts: SshAccessPart[]) => {
    changeAccess(() => sshAccessApi.clear(clearTarget, parts), t('sshAccess.removed'))
  }

  const runCheckAction = async (request: () => Promise<unknown>, doneText?: string) => {
    setBusy(true)
    try {
      await request()
      if (doneText) toast.success(doneText)
      await loadCheck()
    } catch (err) {
      toast.error(failureText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <motion.div
        className="card w-full max-w-3xl max-h-[90vh] flex flex-col overflow-y-auto"
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
      >
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
            <KeyRound className="w-5 h-5 text-accent-500" />
            {t('sshAccess.title')}
          </h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-200" aria-label={t('common.close')}>
            <X className="w-5 h-5" />
          </button>
        </div>
        <p className="text-sm text-dark-400 mb-4">{t('sshAccess.desc')}</p>

        <section className="mb-5">
          <h3 className="text-sm font-semibold text-dark-200 mb-2">{t('sshAccess.now')}</h3>
          {overview
            ? <SshAccessOverview overview={overview} disabled={busy} onClear={handleClear} />
            : <Loader2 className="w-5 h-5 animate-spin text-dark-500" />}
        </section>

        <section className="space-y-3 mb-4">
          <h3 className="text-sm font-semibold text-dark-200">{t('sshAccess.set')}</h3>

          <div className="flex items-center gap-x-4 gap-y-1 flex-wrap text-sm">
            <span className="text-dark-400">{t('sshAccess.scope')}:</span>
            {SCOPES.map(option => (
              <label key={option} className="flex items-center gap-1.5 cursor-pointer">
                <input
                  type="radio"
                  name="ssh-access-scope"
                  checked={scope === option}
                  onChange={() => setScope(option)}
                  disabled={busy}
                />
                {t(`sshAccess.scope_${option}`)}
              </label>
            ))}
          </div>

          {scope === 'folders' && (
            folderOptions.length === 0
              ? <p className="text-sm text-dark-400">{t('sshAccess.no_folders')}</p>
              : (
                <div className="flex gap-x-4 gap-y-2 flex-wrap max-h-40 overflow-y-auto">
                  {folderOptions.map(({ name, size }) => (
                    <label key={name} className="flex items-center gap-2 text-sm text-dark-200 cursor-pointer">
                      <Checkbox checked={folders.includes(name)} onChange={() => toggleFolder(name)} disabled={busy} />
                      {`${name} (${size})`}
                    </label>
                  ))}
                </div>
              )
          )}

          {scope === 'servers' && (
            <div className="space-y-2">
              <div className="max-h-72 overflow-y-auto">
                <ServerSelector servers={selectorServers} selectedIds={serverIds} onChange={setServerIds} />
              </div>
              <div className="flex items-center gap-2 flex-wrap text-xs">
                <span className="text-dark-400">{t('sshAccess.remove_selected')}:</span>
                {ALL_ACCESS_PARTS.map(part => (
                  <button
                    key={part}
                    type="button"
                    onClick={() => handleClear({ scope: 'servers', server_ids: serverIds }, [part])}
                    disabled={busy || serverIds.length === 0}
                    className="btn btn-secondary text-xs py-1 px-2"
                  >
                    {t(`sshAccess.remove_${part}`)}
                  </button>
                ))}
              </div>
            </div>
          )}

          <SshCredsFields
            value={form.creds}
            onChange={patch => setForm(prev => ({ ...prev, creds: { ...prev.creds, ...patch } }))}
            disabled={busy}
            showHost={false}
          />
          <SshProxyFields
            proxy={form.proxy}
            mode={form.proxyMode}
            onChange={patch => setForm(prev => ({ ...prev, ...patch }))}
            disabled={busy}
          />
          {errors.port && <p className="text-xs text-danger">{t('sshAccess.port_invalid')}</p>}
          {errors.proxy && <p className="text-xs text-danger">{t('sshAccess.proxy_invalid')}</p>}
          <p className="text-xs text-dark-500">{t('sshAccess.empty_hint')}</p>
        </section>

        {check && (
          <div className="mb-4">
            <SshAccessCheck
              check={check}
              busy={busy}
              onCancel={() => runCheckAction(() => sshAccessApi.cancelCheck())}
              onAcceptHostKeys={ids => runCheckAction(
                () => sshAccessApi.forgetHostKeys(ids), t('sshAccess.host_keys_accepted'),
              )}
            />
          </div>
        )}

        <div className="flex items-center justify-between gap-3 mt-auto flex-wrap">
          <span className="text-xs text-dark-500">{dirty && t('sshAccess.check_needs_save')}</span>
          <div className="flex gap-2 shrink-0">
            <button onClick={onClose} className="btn btn-secondary">
              {t('common.close')}
            </button>
            <button
              onClick={() => runCheckAction(() => sshAccessApi.startCheck(target))}
              className="btn btn-secondary"
              disabled={busy || dirty || !targetChosen || checkRunning}
            >
              <ShieldCheck className="w-4 h-4" />
              {t('sshAccess.check')}
            </button>
            <button onClick={handleSave} className="btn btn-primary" disabled={busy || !dirty || !targetChosen || formInvalid}>
              {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              {t('sshAccess.save')}
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  )
}

// Страница «Обновления» перерисовывается на каждый ответ обхода нод. Окну от неё нужны
// только имена, адреса и папки, поэтому с теми же пропсами оно остаётся как было —
// иначе список из сотен серверов внутри перерисовывался бы несколько раз в секунду
export default memo(SshAccessModal)
