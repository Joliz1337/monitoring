import { useCallback, useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { Bookmark, Globe, Plus, Save, Trash2, X, Loader2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  xrayTestApi,
  type XrayTestSniSet,
  type XrayTestSubscriptionProfile,
} from '../../api/client'
import { Section } from '../../pages/XrayTest'
import { extractError } from './useTestRun'

export function ProfilesTab() {
  const [sources, setSources] = useState<XrayTestSubscriptionProfile[]>([])
  const [sniSets, setSniSets] = useState<XrayTestSniSet[]>([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [subs, sets] = await Promise.all([xrayTestApi.subscriptions(), xrayTestApi.sniSets()])
      setSources(subs.data.profiles)
      setSniSets(sets.data.profiles)
    } catch (error) {
      toast.error(extractError(error))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <Loader2 className="w-5 h-5 animate-spin text-dark-500" />
      </div>
    )
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      className="grid gap-4 lg:grid-cols-2"
    >
      <SourceProfiles profiles={sources} onChanged={load} />
      <SniProfiles profiles={sniSets} onChanged={load} />
    </motion.div>
  )
}

function SourceProfiles({ profiles, onChanged }: {
  profiles: XrayTestSubscriptionProfile[]
  onChanged: () => void
}) {
  const { t } = useTranslation()
  const [editing, setEditing] = useState<number | 'new' | null>(null)
  const [name, setName] = useState('')
  const [kind, setKind] = useState<'url' | 'links'>('url')
  const [payload, setPayload] = useState('')
  const [saving, setSaving] = useState(false)

  const startNew = () => {
    setEditing('new')
    setName('')
    setKind('url')
    setPayload('')
  }

  const startEdit = (profile: XrayTestSubscriptionProfile) => {
    setEditing(profile.id)
    setName(profile.name)
    setKind(profile.kind)
    setPayload(profile.payload)
  }

  const save = async () => {
    if (!name.trim() || !payload.trim()) {
      toast.error(t('xray_test.fill_name_and_payload'))
      return
    }
    setSaving(true)
    try {
      if (editing === 'new') {
        await xrayTestApi.createSubscription({ name: name.trim(), kind, payload: payload.trim() })
      } else if (typeof editing === 'number') {
        await xrayTestApi.updateSubscription(editing, { name: name.trim(), payload: payload.trim() })
      }
      toast.success(t('xray_test.profile_saved'))
      setEditing(null)
      onChanged()
    } catch (error) {
      toast.error(extractError(error))
    } finally {
      setSaving(false)
    }
  }

  const remove = async (profile: XrayTestSubscriptionProfile) => {
    if (!confirm(t('xray_test.confirm_delete', { name: profile.name }))) return
    try {
      await xrayTestApi.deleteSubscription(profile.id)
      toast.success(t('xray_test.profile_deleted'))
      onChanged()
    } catch (error) {
      toast.error(extractError(error))
    }
  }

  return (
    <Section
      title={t('xray_test.saved_sources')}
      icon={<Bookmark className="w-4 h-4" />}
      right={
        <button className="btn btn-secondary text-xs" onClick={startNew}>
          <Plus className="w-3.5 h-3.5" />
          {t('xray_test.add')}
        </button>
      }
    >
      {editing !== null && (
        <div className="mb-4 p-3 rounded-lg border border-dark-800/60 bg-dark-900/40 space-y-2">
          <input
            className="input w-full text-sm"
            placeholder={t('xray_test.profile_name')}
            value={name}
            onChange={event => setName(event.target.value)}
          />
          {editing === 'new' && (
            <div className="flex gap-2 text-xs">
              {(['url', 'links'] as const).map(option => (
                <button
                  key={option}
                  onClick={() => setKind(option)}
                  className={`px-3 py-1 rounded-md ${
                    kind === option ? 'bg-accent-500/15 text-accent-400' : 'text-dark-400 hover:text-dark-200'
                  }`}
                >
                  {t(`xray_test.kind_${option}`)}
                </button>
              ))}
            </div>
          )}
          <textarea
            className="input w-full font-mono text-xs h-24"
            placeholder={kind === 'url' ? 'https://example.com/sub/token' : 'vless://…'}
            value={payload}
            onChange={event => setPayload(event.target.value)}
          />
          <div className="flex gap-2">
            <button className="btn btn-primary text-xs" onClick={save} disabled={saving}>
              {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
              {t('xray_test.save')}
            </button>
            <button className="btn btn-ghost text-xs" onClick={() => setEditing(null)}>
              <X className="w-3.5 h-3.5" />
              {t('xray_test.cancel')}
            </button>
          </div>
        </div>
      )}

      {profiles.length === 0 ? (
        <p className="text-sm text-dark-400 text-center py-6">{t('xray_test.no_sources')}</p>
      ) : (
        <div className="space-y-2">
          {profiles.map(profile => (
            <div
              key={profile.id}
              className="flex items-center gap-3 p-2.5 rounded-lg border border-dark-800/60 hover:border-dark-700/60 transition-colors"
            >
              <div className="flex-1 min-w-0">
                <div className="text-sm text-dark-200 truncate">{profile.name}</div>
                <div className="text-[11px] text-dark-500">
                  {t(`xray_test.kind_${profile.kind}`)}
                  {profile.last_count ? ` · ${t('xray_test.last_count', { count: profile.last_count })}` : ''}
                </div>
              </div>
              <button
                className="text-xs text-dark-400 hover:text-accent-400"
                onClick={() => startEdit(profile)}
              >
                {t('xray_test.edit')}
              </button>
              <button className="text-dark-500 hover:text-red-400" onClick={() => remove(profile)}>
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}
    </Section>
  )
}

function SniProfiles({ profiles, onChanged }: {
  profiles: XrayTestSniSet[]
  onChanged: () => void
}) {
  const { t } = useTranslation()
  const [editing, setEditing] = useState<number | 'new' | null>(null)
  const [name, setName] = useState('')
  const [text, setText] = useState('')
  const [saving, setSaving] = useState(false)

  const save = async () => {
    const list = text.split(/[\s,;]+/).map(item => item.trim()).filter(Boolean)
    if (!name.trim() || !list.length) {
      toast.error(t('xray_test.fill_name_and_sni'))
      return
    }
    setSaving(true)
    try {
      if (editing === 'new') {
        await xrayTestApi.createSniSet({ name: name.trim(), sni_list: list })
      } else if (typeof editing === 'number') {
        await xrayTestApi.updateSniSet(editing, { name: name.trim(), sni_list: list })
      }
      toast.success(t('xray_test.profile_saved'))
      setEditing(null)
      onChanged()
    } catch (error) {
      toast.error(extractError(error))
    } finally {
      setSaving(false)
    }
  }

  const remove = async (profile: XrayTestSniSet) => {
    if (!confirm(t('xray_test.confirm_delete', { name: profile.name }))) return
    try {
      await xrayTestApi.deleteSniSet(profile.id)
      toast.success(t('xray_test.profile_deleted'))
      onChanged()
    } catch (error) {
      toast.error(extractError(error))
    }
  }

  return (
    <Section
      title={t('xray_test.saved_sni_sets')}
      icon={<Globe className="w-4 h-4" />}
      right={
        <button
          className="btn btn-secondary text-xs"
          onClick={() => { setEditing('new'); setName(''); setText('') }}
        >
          <Plus className="w-3.5 h-3.5" />
          {t('xray_test.add')}
        </button>
      }
    >
      {editing !== null && (
        <div className="mb-4 p-3 rounded-lg border border-dark-800/60 bg-dark-900/40 space-y-2">
          <input
            className="input w-full text-sm"
            placeholder={t('xray_test.profile_name')}
            value={name}
            onChange={event => setName(event.target.value)}
          />
          <textarea
            className="input w-full font-mono text-xs h-24"
            placeholder="www.microsoft.com&#10;www.apple.com"
            value={text}
            onChange={event => setText(event.target.value)}
          />
          <div className="flex gap-2">
            <button className="btn btn-primary text-xs" onClick={save} disabled={saving}>
              {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
              {t('xray_test.save')}
            </button>
            <button className="btn btn-ghost text-xs" onClick={() => setEditing(null)}>
              <X className="w-3.5 h-3.5" />
              {t('xray_test.cancel')}
            </button>
          </div>
        </div>
      )}

      {profiles.length === 0 ? (
        <p className="text-sm text-dark-400 text-center py-6">{t('xray_test.no_sni_sets')}</p>
      ) : (
        <div className="space-y-2">
          {profiles.map(profile => (
            <div
              key={profile.id}
              className="flex items-center gap-3 p-2.5 rounded-lg border border-dark-800/60 hover:border-dark-700/60 transition-colors"
            >
              <div className="flex-1 min-w-0">
                <div className="text-sm text-dark-200 truncate">{profile.name}</div>
                <div className="text-[11px] text-dark-500 truncate">
                  {profile.sni_list.join(', ')}
                </div>
              </div>
              <button
                className="text-xs text-dark-400 hover:text-accent-400"
                onClick={() => {
                  setEditing(profile.id)
                  setName(profile.name)
                  setText(profile.sni_list.join('\n'))
                }}
              >
                {t('xray_test.edit')}
              </button>
              <button className="text-dark-500 hover:text-red-400" onClick={() => remove(profile)}>
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}
    </Section>
  )
}
