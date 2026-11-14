import { useState, type FormEvent, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Plus, X } from 'lucide-react'

interface Props {
  addresses: string[]
  onAdd: (addresses: string[]) => void
  onRemove: (address: string) => void
  renderOptions?: (address: string, index: number) => ReactNode
}

export default function AddressList({ addresses, onAdd, onRemove, renderOptions }: Props) {
  const { t } = useTranslation()
  const [newAddresses, setNewAddresses] = useState('')

  // Вставленный списком текст разбирается по пробелам, запятым и строкам; адрес
  // проверяет панель при сохранении
  const addTyped = (e: FormEvent) => {
    e.preventDefault()
    const known = new Set(addresses)
    onAdd([...new Set(newAddresses.split(/[\s,]+/).filter(Boolean))].filter(address => !known.has(address)))
    setNewAddresses('')
  }

  return (
    <>
      {addresses.length > 0 && (
        <div className="space-y-1.5">
          {addresses.map((address, index) => (
            <div
              key={address}
              className="flex items-center gap-3 flex-wrap px-3 py-2 rounded-lg bg-dark-800/50 border border-dark-700/50"
            >
              <span className="font-mono text-sm text-dark-100 flex-1 min-w-[10rem] break-all">{address}</span>
              {renderOptions?.(address, index)}
              <button
                onClick={() => onRemove(address)}
                title={t('loss.excluded_target_remove')}
                className="p-1.5 rounded-md text-dark-500 hover:text-danger hover:bg-danger/10 transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          ))}
        </div>
      )}
      <form onSubmit={addTyped} className="flex gap-2">
        <input
          type="text"
          value={newAddresses}
          onChange={e => setNewAddresses(e.target.value)}
          placeholder="62.50.146.225:8443"
          className="input font-mono text-sm flex-1 min-w-0"
        />
        <button
          type="submit"
          disabled={!newAddresses.trim()}
          className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm text-accent-400 hover:bg-accent-500/10 transition-colors disabled:opacity-40"
        >
          <Plus className="w-4 h-4" /> {t('loss.excluded_target_add')}
        </button>
      </form>
    </>
  )
}
