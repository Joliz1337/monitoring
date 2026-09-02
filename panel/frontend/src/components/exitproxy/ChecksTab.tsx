import { useState } from 'react'
import { Loader2, Pencil, Plus, Trash2, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import type { ExitProxyBuiltinCheckKey, ExitProxyCustomCheck, ExitProxyCustomCheckInput } from '../../api/client'
import { useExitProxyStore } from '../../stores/exitProxyStore'
import { Toggle } from '../ui/Toggle'

interface FormState {
  name: string
  url: string
  block_status: string
  block_regex: string
  block_url_regex: string
  expect_status: string
}

const EMPTY: FormState = { name: '', url: '', block_status: '', block_regex: '', block_url_regex: '', expect_status: '' }
const BUILTIN_KEYS: ExitProxyBuiltinCheckKey[] = ['google_country', 'google_captcha', 'gemini']

function toInput(form: FormState, enabled: boolean): ExitProxyCustomCheckInput | null {
  const url = form.url.trim()
  if (!/^https?:\/\//.test(url)) return null
  const codes = form.block_status.split(/[,\s]+/).map(s => parseInt(s, 10)).filter(n => n >= 100 && n <= 599)
  const expect = parseInt(form.expect_status, 10)
  return {
    name: form.name.trim() || url,
    url,
    enabled,
    block_status: codes,
    block_regex: form.block_regex.trim(),
    block_url_regex: form.block_url_regex.trim(),
    expect_status: expect >= 100 && expect <= 599 ? expect : null,
  }
}

function toForm(check: ExitProxyCustomCheck): FormState {
  return {
    name: check.name,
    url: check.url,
    block_status: check.block_status.join(', '),
    block_regex: check.block_regex,
    block_url_regex: check.block_url_regex,
    expect_status: check.expect_status ? String(check.expect_status) : '',
  }
}

export default function ChecksTab() {
  const { t } = useTranslation()
  const checks = useExitProxyStore(s => s.checks)
  const setBuiltinCheck = useExitProxyStore(s => s.setBuiltinCheck)
  const addCheck = useExitProxyStore(s => s.addCheck)
  const updateCheck = useExitProxyStore(s => s.updateCheck)
  const deleteCheck = useExitProxyStore(s => s.deleteCheck)
  const isBusy = useExitProxyStore(s => s.isBusy)
  const [form, setForm] = useState<FormState>(EMPTY)
  const [editing, setEditing] = useState<ExitProxyCustomCheck | null>(null)

  const field = (key: keyof FormState) => ({
    value: form[key],
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => setForm(prev => ({ ...prev, [key]: e.target.value })),
  })

  const submit = async () => {
    const input = toInput(form, editing ? editing.enabled : true)
    if (!input) {
      toast.error(t('exit_proxy.check_invalid_url'))
      return
    }
    const ok = editing ? await updateCheck(editing.id, input) : await addCheck(input)
    if (ok) {
      setForm(EMPTY)
      setEditing(null)
    }
  }

  const startEdit = (check: ExitProxyCustomCheck) => {
    setEditing(check)
    setForm(toForm(check))
  }

  const cancelEdit = () => {
    setEditing(null)
    setForm(EMPTY)
  }

  const builtin = new Map(checks?.builtin.map(item => [item.key, item.enabled]) ?? [])
  const submitting = isBusy('check-add') || (editing ? isBusy(`check-${editing.id}`) : false)

  return (
    <div className="space-y-4">
      <div className="card p-5 space-y-3">
        <div>
          <h3 className="text-sm font-medium text-dark-200">{t('exit_proxy.builtin_title')}</h3>
          <p className="text-xs text-dark-500 mt-1">{t('exit_proxy.builtin_desc')}</p>
        </div>
        <div className="divide-y divide-dark-800">
          {BUILTIN_KEYS.map(key => (
            <div key={key} className="flex items-center justify-between py-2.5">
              <div>
                <p className="text-sm text-dark-200">{t(`exit_proxy.check_${key}`)}</p>
                <p className="text-xs text-dark-500">{t(`exit_proxy.check_${key}_desc`)}</p>
              </div>
              <Toggle on={builtin.get(key) ?? true} onClick={() => setBuiltinCheck(key, !(builtin.get(key) ?? true))} disabled={isBusy(`builtin-${key}`)} />
            </div>
          ))}
        </div>
      </div>

      <div className="card p-5 space-y-4">
        <div>
          <h3 className="text-sm font-medium text-dark-200">{t('exit_proxy.custom_title')}</h3>
          <p className="text-xs text-dark-500 mt-1">{t('exit_proxy.custom_desc')}</p>
        </div>

        <div className="grid sm:grid-cols-2 lg:grid-cols-6 gap-2">
          <input {...field('name')} placeholder={t('exit_proxy.check_name_placeholder')} className="input lg:col-span-1" />
          <input {...field('url')} placeholder={t('exit_proxy.check_url_placeholder')} className="input font-mono lg:col-span-2" onKeyDown={e => e.key === 'Enter' && submit()} />
          <input {...field('block_status')} placeholder={t('exit_proxy.check_block_status_placeholder')} className="input font-mono" />
          <input {...field('block_url_regex')} placeholder={t('exit_proxy.check_url_regex_placeholder')} className="input font-mono" />
          <input {...field('block_regex')} placeholder={t('exit_proxy.check_regex_placeholder')} className="input font-mono" />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input {...field('expect_status')} placeholder={t('exit_proxy.check_expect_placeholder')} className="input font-mono w-32" />
          <button onClick={submit} disabled={submitting} className="btn btn-primary text-xs">
            {submitting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : editing ? <Pencil className="w-3.5 h-3.5" /> : <Plus className="w-3.5 h-3.5" />}
            {editing ? t('exit_proxy.check_save') : t('exit_proxy.check_add')}
          </button>
          {editing && (
            <button onClick={cancelEdit} className="btn btn-secondary text-xs">
              <X className="w-3.5 h-3.5" />
              {t('exit_proxy.check_cancel')}
            </button>
          )}
          <span className="text-[11px] text-dark-500">{t('exit_proxy.check_form_hint')}</span>
        </div>

        {!checks || checks.custom.length === 0 ? (
          <p className="text-xs text-dark-500">{t('exit_proxy.check_empty')}</p>
        ) : (
          <div className="divide-y divide-dark-800">
            {checks.custom.map(check => (
              <div key={check.id} className="flex flex-wrap items-center gap-3 py-2.5">
                <div className="flex-1 min-w-[220px]">
                  <p className="text-sm text-dark-200">{check.name}</p>
                  <p className="text-xs text-dark-500 font-mono break-all">{check.url}</p>
                  <p className="text-[11px] text-dark-500">
                    {check.block_status.length > 0 && <>{t('exit_proxy.check_block_status_label')}: {check.block_status.join(', ')} · </>}
                    {check.block_url_regex && <>{t('exit_proxy.check_url_regex_label')}: <span className="font-mono">{check.block_url_regex}</span> · </>}
                    {check.block_regex && <>{t('exit_proxy.check_regex_label')}: <span className="font-mono">{check.block_regex}</span> · </>}
                    {check.expect_status && <>{t('exit_proxy.check_expect_label')}: {check.expect_status}</>}
                  </p>
                </div>
                <Toggle
                  on={check.enabled}
                  disabled={isBusy(`check-${check.id}`)}
                  onClick={() => updateCheck(check.id, { ...check, enabled: !check.enabled })}
                />
                <button onClick={() => startEdit(check)} className="text-dark-500 hover:text-dark-200" title={t('exit_proxy.check_edit')}>
                  <Pencil className="w-4 h-4" />
                </button>
                <button onClick={() => deleteCheck(check.id)} disabled={isBusy(`check-${check.id}`)} className="text-dark-500 hover:text-red-400" title={t('exit_proxy.check_delete')}>
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
