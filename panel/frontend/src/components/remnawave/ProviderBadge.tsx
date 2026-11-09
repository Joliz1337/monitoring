import { useTranslation } from 'react-i18next'
import { Tooltip } from '../ui/Tooltip'
import { providerBrand } from './geo'

interface ProviderBadgeProps {
  asOrg: string | null
  asn: number | null
}

export function ProviderBadge({ asOrg, asn }: ProviderBadgeProps) {
  const { i18n } = useTranslation()
  const brand = providerBrand(asOrg, asn, i18n.language)
  if (!brand) return <span className="text-dark-600">—</span>

  const fullName = [asOrg, asn ? `AS${asn}` : null].filter(Boolean).join(' · ')
  return (
    <Tooltip label={fullName}>
      <span className="inline-flex items-center gap-2 min-w-0 max-w-full">
        <span
          className={`w-5 h-5 rounded-md flex items-center justify-center font-bold shrink-0 ring-1 ring-white/10 ${
            brand.mark.length > 2 ? 'text-[7px]' : 'text-[10px]'
          }`}
          style={{ background: brand.bg, color: brand.fg }}
        >
          {brand.mark}
        </span>
        <span className="truncate text-dark-200">{brand.label}</span>
      </span>
    </Tooltip>
  )
}
