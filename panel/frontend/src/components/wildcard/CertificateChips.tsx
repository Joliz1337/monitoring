import { Check } from 'lucide-react'
import type { WildcardCertificate } from '../../api/client'

interface Props {
  certificates: WildcardCertificate[]
  selected: number[]
  onChange: (ids: number[]) => void
  // Свои абсолютные пути вмещают одну пару файлов — выбор как у радиокнопок
  single?: boolean
  disabled?: boolean
}

// Выбор сертификатов, которые раскатываются на сервер
export default function CertificateChips({
  certificates,
  selected,
  onChange,
  single = false,
  disabled = false,
}: Props) {
  const toggle = (id: number) => {
    if (selected.includes(id)) {
      onChange(selected.filter(x => x !== id))
      return
    }
    onChange(single ? [id] : certificates.map(c => c.id).filter(x => x === id || selected.includes(x)))
  }

  return (
    <div className="flex flex-wrap gap-2">
      {certificates.map(cert => {
        const active = selected.includes(cert.id)
        return (
          <button
            key={cert.id}
            type="button"
            onClick={() => toggle(cert.id)}
            disabled={disabled}
            className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-xs font-mono transition-colors disabled:opacity-50 ${
              active
                ? 'border-accent-500/50 bg-accent-500/10 text-accent-300'
                : 'border-dark-700/50 bg-dark-800/50 text-dark-400 hover:text-dark-100'
            }`}
          >
            {active && <Check className="w-3.5 h-3.5" />}
            {cert.domain}
          </button>
        )
      })}
    </div>
  )
}
