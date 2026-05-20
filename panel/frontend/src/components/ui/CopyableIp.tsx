import { useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Tooltip } from './Tooltip'

interface CopyableIpProps {
  value: string
  display?: ReactNode
  className?: string
}

async function copyToClipboard(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const textArea = document.createElement('textarea')
    textArea.value = text
    textArea.style.position = 'fixed'
    textArea.style.opacity = '0'
    document.body.appendChild(textArea)
    textArea.select()
    document.execCommand('copy')
    document.body.removeChild(textArea)
  }
}

/**
 * Текст IP-адреса, который копируется в буфер по клику ЛКМ.
 * При наведении показывает подсказку, после клика — «Скопировано».
 */
export function CopyableIp({ value, display, className = '' }: CopyableIpProps) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)

  const handleCopy = async (e: React.MouseEvent | React.KeyboardEvent) => {
    e.stopPropagation()
    e.preventDefault()
    await copyToClipboard(value)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <Tooltip label={copied ? t('common.copied') : t('common.copy_ip')}>
      <span
        role="button"
        tabIndex={0}
        onClick={handleCopy}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') handleCopy(e)
        }}
        className={`cursor-pointer transition-colors hover:text-accent-400 ${copied ? 'text-success' : ''} ${className}`}
      >
        {display ?? value}
      </span>
    </Tooltip>
  )
}
