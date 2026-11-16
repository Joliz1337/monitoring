import { Fragment, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { Loader2, Server } from 'lucide-react'
import QuarantineAddressRow, { type AddressActions } from './QuarantineAddressRow'
import { releaseItems } from './quarantineRelease'
import { releasable, type AddressGroup, type NodeGroup } from './quarantineGroups'

// У ноды бывают десятки отключённых адресов — карточка показывает первые
const ADDRESSES_SHOWN = 10

interface Props extends AddressActions {
  node: NodeGroup
  mergePorts: boolean
  now: number
}

interface ReleaseAllProps {
  addresses: AddressGroup[]
  onReleased: () => void
}

// Возврат разом пускает клиентов на все больные адреса ноды, а панель час не отключит их заново —
// поэтому со второго нажатия
function ReleaseAllButton({ addresses, onReleased }: ReleaseAllProps) {
  const { t } = useTranslation()
  const [confirming, setConfirming] = useState(false)
  const [releasing, setReleasing] = useState(false)
  const count = addresses.length

  const release = async () => {
    if (!confirming) {
      setConfirming(true)
      return
    }
    setConfirming(false)
    setReleasing(true)
    const released = await releaseItems(addresses.flatMap(address => releasable(address.items)))
    setReleasing(false)
    if (released) toast.success(t('loss.quarantine_released_many', { count }))
    else toast.error(t('loss.quarantine_release_failed_many'))
    onReleased()
  }

  return (
    <button
      type="button"
      onClick={release}
      onBlur={() => setConfirming(false)}
      disabled={releasing}
      title={t('loss.quarantine_release_hint')}
      className={`ml-auto shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs transition-colors disabled:opacity-40 ${
        confirming ? 'text-warning bg-warning/10' : 'text-accent-400 hover:bg-accent-500/10'
      }`}
    >
      {releasing && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
      {t(confirming ? 'loss.quarantine_release_all_confirm' : 'loss.quarantine_release_all', { count })}
    </button>
  )
}

function countWith(addresses: AddressGroup[], state: 'would_disable' | 'blocked'): number {
  return addresses.filter(address => address.items.some(item => item.state === state)).length
}

export default function QuarantineNodeCard({ node, mergePorts, now, ...actions }: Props) {
  const { t } = useTranslation()
  const [showAll, setShowAll] = useState(false)
  const name = node.owner ?? t('loss.owner_unknown')
  const returnable = node.addresses.filter(address => releasable(address.items).length > 0)
  const wouldDisable = countWith(node.addresses, 'would_disable')
  const refused = countWith(node.addresses, 'blocked')
  const shown = showAll ? node.addresses : node.addresses.slice(0, ADDRESSES_SHOWN)
  // Сводка под именем ноды: каждое число переносится на новую строку целиком
  const facts = [{ text: t('loss.quarantine_node_addresses', { count: node.addresses.length }), alarming: false }]
  if (returnable.length > 0) facts.push({ text: t('loss.quarantine_node_disabled', { count: returnable.length }), alarming: false })
  if (wouldDisable > 0) facts.push({ text: t('loss.quarantine_node_would_disable', { count: wouldDisable }), alarming: false })
  if (refused > 0) facts.push({ text: t('loss.quarantine_node_blocked', { count: refused }), alarming: true })

  return (
    <section aria-label={name} className="bg-dark-900/50 rounded-xl border border-dark-800/50">
      <header className="flex items-center gap-3 pl-4 pr-3 py-3.5">
        <div className="w-9 h-9 rounded-[10px] shrink-0 flex items-center justify-center bg-dark-800/60 text-dark-300">
          <Server className="w-[18px] h-[18px]" />
        </div>
        <div className="min-w-0">
          <h3 className="text-[15px] font-semibold leading-tight text-dark-100 truncate">{name}</h3>
          <p className="text-xs text-dark-400">
            {facts.map(({ text, alarming }, index) => (
              <Fragment key={text}>
                {index > 0 && ' · '}
                <span className={`whitespace-nowrap ${alarming ? 'text-danger' : ''}`}>{text}</span>
              </Fragment>
            ))}
          </p>
        </div>
        {returnable.length > 1 && <ReleaseAllButton addresses={returnable} onReleased={actions.onReleased} />}
      </header>
      <ul>
        {shown.map(address => (
          <QuarantineAddressRow key={address.key} group={address} mergePorts={mergePorts} now={now} {...actions} />
        ))}
      </ul>
      {node.addresses.length > ADDRESSES_SHOWN && (
        <button
          type="button"
          onClick={() => setShowAll(prev => !prev)}
          className="w-full px-4 py-2 rounded-b-xl border-t border-dark-800/50 text-xs text-accent-400 hover:bg-accent-500/10 transition-colors"
        >
          {showAll ? t('loss.quarantine_show_less') : t('loss.quarantine_show_all', { count: node.addresses.length })}
        </button>
      )}
    </section>
  )
}
