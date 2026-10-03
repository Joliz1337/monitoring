import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { BackendServer } from '../../api/client'
import { parseServerLines } from '../../utils/haproxyServerLines'

export type ServersMergeMode = 'append' | 'replace'

const UNRECOGNIZED_LINES_SHOWN = 10

export default function ServersPasteBox({
  defaults, onApply, onClose,
}: {
  defaults: BackendServer
  onApply: (servers: BackendServer[], mode: ServersMergeMode) => void
  onClose: () => void
}) {
  const { t } = useTranslation()
  const [text, setText] = useState('')
  const { servers, unrecognizedLines } = parseServerLines(text, defaults)

  const shownLines = unrecognizedLines.slice(0, UNRECOGNIZED_LINES_SHOWN).join(', ')
  const unrecognizedLabel = unrecognizedLines.length > UNRECOGNIZED_LINES_SHOWN ? `${shownLines}…` : shownLines
  const actionButton = 'px-3 py-1.5 rounded-lg text-xs transition-colors disabled:opacity-50 disabled:cursor-not-allowed'

  return (
    <div className="p-3 mb-2 bg-dark-900/40 rounded-lg border border-dark-700/40 space-y-2">
      <textarea value={text} onChange={e => setText(e.target.value)} autoFocus spellCheck={false} rows={4}
        placeholder={'server srv1 1.2.3.4:443 weight 2 check\n5.6.7.8:443, 9.10.11.12:443'}
        className="w-full px-3 py-2 rounded-lg bg-dark-950 border border-dark-700 text-dark-200 text-xs font-mono focus:outline-none focus:border-accent-500/50 resize-y" />
      <p className="text-2xs text-dark-500">{t('balancer.paste_hint')}</p>
      {text.trim() && (
        <p className="text-2xs text-dark-400">
          {t('balancer.paste_recognized', { count: servers.length })}
          {unrecognizedLines.length > 0 && (
            <span className="text-amber-400"> · {t('balancer.paste_unrecognized', { lines: unrecognizedLabel })}</span>
          )}
        </p>
      )}
      <div className="flex flex-wrap justify-end gap-2">
        <button type="button" onClick={onClose}
          className={`${actionButton} text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700`}>
          {t('common.cancel')}
        </button>
        <button type="button" onClick={() => onApply(servers, 'append')} disabled={servers.length === 0}
          className={`${actionButton} text-dark-200 bg-dark-800 hover:bg-dark-700 border border-dark-700`}>
          {t('balancer.paste_append')}
        </button>
        <button type="button" onClick={() => onApply(servers, 'replace')} disabled={servers.length === 0}
          className={`${actionButton} font-medium bg-accent-600 hover:bg-accent-500 text-white`}>
          {t('balancer.paste_replace')}
        </button>
      </div>
    </div>
  )
}
