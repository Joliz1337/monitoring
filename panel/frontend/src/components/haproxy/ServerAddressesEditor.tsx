import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { AlertTriangle, Loader2, Save } from 'lucide-react'
import { haproxyProfilesApi, proxyApi, type HAProxyServerStatus } from '../../api/client'
import { Checkbox } from '../ui/Checkbox'

interface NodeAddress {
  address: string
  iface: string
  primary: boolean
}

type NodeAddressesState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; addresses: NodeAddress[] }
  | { status: 'error'; message: string }

interface AddressRow {
  address: string
  iface: string | null
  primary: boolean
  missing: boolean
}

function sameAddresses(selected: Set<string>, saved: string[]): boolean {
  return selected.size === saved.length && saved.every(ip => selected.has(ip))
}

function toggled(selected: Set<string>, ip: string): Set<string> {
  const next = new Set(selected)
  if (next.has(ip)) next.delete(ip)
  else next.add(ip)
  return next
}

export default function ServerAddressesEditor({ profileId, server, onSaved }: {
  profileId: number
  server: HAProxyServerStatus
  onSaved: () => void
}) {
  const { t } = useTranslation()
  const [nodeAddresses, setNodeAddresses] = useState<NodeAddressesState>({ status: 'idle' })
  const [listen, setListen] = useState(() => new Set(server.listen_ips))
  const [source, setSource] = useState(() => new Set(server.source_ips))
  const [saving, setSaving] = useState(false)

  const canLoad = server.addresses_supported && server.online
  const unsupportedText = t('haproxy_configs.addresses_unsupported', { version: server.addresses_min_node_version })

  useEffect(() => {
    if (!canLoad) return
    let cancelled = false
    setNodeAddresses({ status: 'loading' })
    proxyApi.getNetworkState(server.server_id)
      .then(res => {
        if (cancelled) return
        if (!res.data.supported) {
          setNodeAddresses({ status: 'error', message: unsupportedText })
          return
        }
        const found = new Map<string, NodeAddress>()
        for (const iface of res.data.interfaces) {
          for (const addr of iface.addresses) {
            if (addr.family === 'ipv4' && !found.has(addr.address)) {
              found.set(addr.address, { address: addr.address, iface: iface.name, primary: addr.primary })
            }
          }
        }
        setNodeAddresses({ status: 'ready', addresses: [...found.values()] })
      })
      .catch((err: any) => {
        if (!cancelled) {
          setNodeAddresses({ status: 'error', message: err?.response?.data?.detail || t('haproxy_configs.addresses_load_error') })
        }
      })
    return () => { cancelled = true }
  }, [canLoad, server.server_id, unsupportedText, t])

  // Сохранённые адреса видны, даже если их уже нет на сервере или список не загрузился, — чтобы их можно было снять
  const nodeRows = nodeAddresses.status === 'ready' ? nodeAddresses.addresses : []
  const known = new Set(nodeRows.map(a => a.address))
  const rows: AddressRow[] = [
    ...nodeRows.map(a => ({ ...a, missing: false })),
    ...[...new Set([...server.listen_ips, ...server.source_ips])]
      .filter(ip => !known.has(ip))
      .map(ip => ({ address: ip, iface: null, primary: false, missing: nodeAddresses.status === 'ready' })),
  ]

  const notice = !server.addresses_supported
    ? unsupportedText
    : !server.online
      ? t('haproxy_configs.addresses_offline')
      : nodeAddresses.status === 'error'
        ? nodeAddresses.message
        : nodeAddresses.status === 'ready' && nodeRows.length === 0
          ? t('haproxy_configs.addresses_no_ipv4')
          : null

  const dirty = !sameAddresses(listen, server.listen_ips) || !sameAddresses(source, server.source_ips)

  const handleSave = async () => {
    // Порядок как в списке: первый отмеченный выходной IP получает исходное имя server-строки
    const ordered = (selected: Set<string>) => rows.map(r => r.address).filter(ip => selected.has(ip))
    setSaving(true)
    try {
      const res = await haproxyProfilesApi.updateServerAddresses(profileId, server.server_id, {
        listen_ips: ordered(listen),
        source_ips: ordered(source),
      })
      const sync = res.data.sync
      if (!sync || sync.status === 'success') toast.success(t('haproxy_configs.addresses_saved'))
      else if (sync.status === 'queued') toast.info(t('haproxy_configs.sync_one_queued'))
      else toast.error(sync.message)
      onSaved()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('haproxy_configs.addresses_save_error'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mt-1.5 rounded-lg border border-dark-700/50 bg-dark-900/50 p-3 space-y-3">
      <div>
        <div className="text-xs font-medium text-dark-200">{t('haproxy_configs.addresses_title')}</div>
        <p className="text-xs text-dark-500 mt-1">{t('haproxy_configs.addresses_hint')}</p>
      </div>

      {notice && (
        <div className="flex items-start gap-2 text-xs text-yellow-400/90">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
          <span>{notice}</span>
        </div>
      )}

      {nodeAddresses.status === 'loading' && (
        <div className="flex justify-center py-3"><Loader2 className="w-4 h-4 text-accent-400 animate-spin" /></div>
      )}

      {rows.length > 0 && (
        <>
          <div className="rounded-lg border border-dark-800/60 divide-y divide-dark-800/60">
            <div className="grid grid-cols-[1fr_4.5rem_4.5rem] gap-x-2 px-3 py-1.5 text-[11px] text-dark-500">
              <span>{t('haproxy_configs.addresses_col_ip')}</span>
              <span className="text-center">{t('haproxy_configs.addresses_col_listen')}</span>
              <span className="text-center">{t('haproxy_configs.addresses_col_source')}</span>
            </div>
            {rows.map(row => (
              <div key={row.address} className="grid grid-cols-[1fr_4.5rem_4.5rem] gap-x-2 items-center px-3 py-2">
                <span className="min-w-0 flex flex-wrap items-center gap-x-2 gap-y-0.5">
                  <span className={`font-mono text-sm ${row.missing ? 'text-dark-500 line-through' : 'text-dark-100'}`}>{row.address}</span>
                  {row.iface && <span className="text-xs text-dark-500">{row.iface}</span>}
                  {row.primary && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-dark-700/60 text-dark-300">{t('haproxy_configs.addresses_primary')}</span>
                  )}
                  {row.missing && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-500/10 text-red-400">{t('haproxy_configs.addresses_missing')}</span>
                  )}
                </span>
                <span className="flex justify-center">
                  <Checkbox checked={listen.has(row.address)} onChange={() => setListen(prev => toggled(prev, row.address))} />
                </span>
                <span className="flex justify-center">
                  <Checkbox checked={source.has(row.address)} onChange={() => setSource(prev => toggled(prev, row.address))} />
                </span>
              </div>
            ))}
          </div>

          <div className="flex justify-end">
            <button onClick={handleSave} disabled={!dirty || saving}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50">
              {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />} {t('common.save')}
            </button>
          </div>
        </>
      )}
    </div>
  )
}
