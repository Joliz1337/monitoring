import { useMemo, useState } from 'react'
import { motion } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, ChevronDown, ClipboardPaste, Loader2 } from 'lucide-react'
import type { GreTunnelInput } from '../../api/client'
import { isIpv4, isPtpPair, parseDdgConnection, ptpPeer } from '../../utils/gre'

interface Props {
  initial: GreTunnelInput
  /** IPv4 интерфейсов сервера, основной первым; null — список получить не удалось */
  serverAddresses: string[] | null
  saving: boolean
  onSave: (tunnel: GreTunnelInput) => void
  onCancel: () => void
}

const inputClass = 'input py-2 font-mono text-sm [&>option]:bg-dark-900'

// Со страницы DDoS-Guard PE и CE копируются с маской /30 — адресу она не нужна
const withoutMask = (value: string) => value.trim().split('/')[0]

export default function GreTunnelForm({ initial, serverAddresses, saving, onSave, onCancel }: Props) {
  const { t } = useTranslation()
  const [ce, setCe] = useState(initial.ce)
  const [pe, setPe] = useState(initial.pe ?? '')
  // PE подставляется из CE, пока его не правили руками
  const [peTouched, setPeTouched] = useState(!!initial.pe && initial.pe !== ptpPeer(initial.ce))
  const [local, setLocal] = useState(initial.local || serverAddresses?.[0] || '')
  const [remote, setRemote] = useState(initial.remote)
  const [healthNet, setHealthNet] = useState(initial.health_net)
  const [pasteOpen, setPasteOpen] = useState(!initial.ce)
  const [pasteText, setPasteText] = useState('')
  const [advancedOpen, setAdvancedOpen] = useState(false)

  const changeCe = (value: string) => {
    const address = withoutMask(value)
    setCe(address)
    if (!peTouched) setPe(ptpPeer(address) ?? '')
  }

  const applyPaste = () => {
    const parsed = parseDdgConnection(pasteText)
    if (parsed.ce) {
      setCe(parsed.ce)
      setPe(parsed.pe ?? ptpPeer(parsed.ce) ?? '')
      setPeTouched(!!parsed.pe && parsed.pe !== ptpPeer(parsed.ce))
    }
    if (parsed.local) setLocal(parsed.local)
    if (parsed.remote) setRemote(parsed.remote)
    if (parsed.health_net) setHealthNet(parsed.health_net)
    setPasteOpen(false)
  }

  const errors = useMemo(() => {
    const found: Partial<Record<'ce' | 'pe' | 'local' | 'remote', string>> = {}
    if (ce && !isIpv4(ce)) found.ce = t('gre.err_ipv4')
    else if (ce && !ptpPeer(ce)) found.ce = t('gre.err_ce_host')
    if (pe && ce && isIpv4(pe) && ptpPeer(ce) && !isPtpPair(pe, ce)) found.pe = t('gre.err_pair')
    else if (pe && !isIpv4(pe)) found.pe = t('gre.err_ipv4')
    if (local && !isIpv4(local)) found.local = t('gre.err_ipv4')
    if (remote && !isIpv4(remote)) found.remote = t('gre.err_ipv4')
    return found
  }, [ce, pe, local, remote, t])

  // GRE SRC со страницы DDoS-Guard, которого нет на интерфейсах: сервер за NAT облака
  const localMissing = !!serverAddresses && !!local && isIpv4(local) && !serverAddresses.includes(local)
  const complete = isIpv4(ce) && isIpv4(pe) && isIpv4(local) && isIpv4(remote) && !!healthNet.trim()
  const canSave = complete && Object.keys(errors).length === 0 && !saving

  const submit = () => {
    if (!canSave) return
    onSave({ slot: initial.slot, ce: ce.trim(), pe: pe.trim(), local: local.trim(), remote: remote.trim(), health_net: healthNet.trim() })
  }

  const field = (label: string, hint: string | null, error: string | undefined, input: JSX.Element) => (
    <label className="block">
      <span className="text-xs text-dark-400">{label}</span>
      <div className="mt-1">{input}</div>
      {error ? <span className="text-2xs text-danger mt-1 block">{error}</span>
        : hint && <span className="text-2xs text-dark-500 mt-1 block">{hint}</span>}
    </label>
  )

  return (
    <motion.div className="card border border-accent-500/30" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
      <h2 className="text-dark-100 font-medium mb-3">
        {initial.slot ? t('gre.form_edit_title', { name: `gre-ddg${initial.slot}` }) : t('gre.form_add_title')}
      </h2>

      <div className="mb-4 rounded-xl border border-dark-700/60">
        <button
          type="button"
          onClick={() => setPasteOpen(open => !open)}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm text-dark-200 hover:text-dark-50"
        >
          <ClipboardPaste className="w-4 h-4 text-accent-400" />
          {t('gre.paste_title')}
          <ChevronDown className={`w-4 h-4 ml-auto transition-transform ${pasteOpen ? 'rotate-180' : ''}`} />
        </button>
        {pasteOpen && (
          <div className="px-3 pb-3">
            <p className="text-2xs text-dark-500 mb-2">{t('gre.paste_hint')}</p>
            <textarea
              value={pasteText}
              onChange={e => setPasteText(e.target.value)}
              rows={5}
              className="input py-2 font-mono text-xs"
              placeholder={'PE: 91.215.40.173/30\nCE: 91.215.40.174/30\nGRE SRC: 178.214.103.5\nGRE DST: 185.129.101.255'}
            />
            <button type="button" className="btn btn-secondary mt-2" disabled={!pasteText.trim()} onClick={applyPaste}>
              {t('gre.paste_apply')}
            </button>
          </div>
        )}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        {field(t('gre.field_ce'), t('gre.field_ce_hint'), errors.ce, (
          <input className={inputClass} value={ce} onChange={e => changeCe(e.target.value)} placeholder="91.215.40.174" />
        ))}
        {field(t('gre.field_pe'), t('gre.field_pe_hint'), errors.pe, (
          <input
            className={inputClass}
            value={pe}
            onChange={e => { setPe(withoutMask(e.target.value)); setPeTouched(true) }}
            placeholder={ptpPeer(ce) ?? '91.215.40.173'}
          />
        ))}
        {field(t('gre.field_local'), t('gre.field_local_hint'), errors.local, serverAddresses && serverAddresses.length > 0 && !localMissing ? (
          <select className={inputClass} value={local} onChange={e => setLocal(e.target.value)}>
            {serverAddresses.map(address => <option key={address} value={address}>{address}</option>)}
          </select>
        ) : (
          <input className={inputClass} value={local} onChange={e => setLocal(e.target.value)} placeholder="178.214.103.5" />
        ))}
        {field(t('gre.field_remote'), t('gre.field_remote_hint'), errors.remote, (
          <input className={inputClass} value={remote} onChange={e => setRemote(e.target.value)} />
        ))}
      </div>

      {localMissing && (
        <div className="mt-3 flex items-start gap-2 px-3 py-2 rounded-xl bg-warning/10 border border-warning/20 text-xs text-dark-300">
          <AlertTriangle className="w-4 h-4 text-warning shrink-0 mt-0.5" />
          <span>{t('gre.local_missing', { address: local, addresses: serverAddresses?.join(', ') || '—' })}</span>
        </div>
      )}

      <button
        type="button"
        onClick={() => setAdvancedOpen(open => !open)}
        className="btn-disclosure mt-3"
      >
        <ChevronDown className={`w-3.5 h-3.5 transition-transform ${advancedOpen ? 'rotate-180' : ''}`} />
        {t('gre.advanced')}
      </button>
      {advancedOpen && (
        <div className="mt-2 sm:w-1/2">
          {field(t('gre.field_health_net'), t('gre.field_health_net_hint'), undefined, (
            <input className={inputClass} value={healthNet} onChange={e => setHealthNet(e.target.value)} />
          ))}
        </div>
      )}

      <div className="flex items-center gap-2 mt-4">
        <button type="button" className="btn btn-primary" disabled={!canSave} onClick={submit}>
          {saving && <Loader2 className="w-4 h-4 animate-spin" />}
          {t('common.save')}
        </button>
        <button type="button" className="btn btn-secondary" disabled={saving} onClick={onCancel}>
          {t('common.cancel')}
        </button>
      </div>
    </motion.div>
  )
}
