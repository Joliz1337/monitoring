/**
 * Реестр облачных провайдеров биллинга.
 *
 * Карточка, формы и калькулятор читают провайдера отсюда — новый провайдер
 * добавляется записью в PROVIDERS, а не ветками по всему разделу.
 */
export type CloudProviderId = 'yandex_cloud' | 'selectel' | 'timeweb'

export interface CloudCredentialField {
  /** Поле модели, куда уходит значение */
  key: 'cloud_account_id' | 'cloud_credential'
  labelKey: string
  hintKey: string
  placeholder: string
  secret?: boolean
  link?: { url: string; label?: string; labelKey?: string }
}

export interface CloudProviderMeta {
  id: CloudProviderId
  nameKey: string
  defaultCurrency: string
  fields: CloudCredentialField[]
  /** Классы Tailwind перечислены целиком: сборщик не видит строки, собранные в рантайме */
  accent: {
    iconBg: string
    icon: string
    badge: string
    primaryButton: string
    ghostButton: string
    quickActive: string
    hintBox: string
  }
}

const YANDEX_ACCENT = {
  iconBg: 'bg-orange-500/20',
  icon: 'text-orange-400',
  badge: 'bg-orange-500/15 text-orange-400',
  primaryButton:
    'bg-gradient-to-r from-orange-500/20 to-amber-500/20 text-orange-400 ' +
    'hover:from-orange-500/30 hover:to-amber-500/30 border border-orange-500/20 ' +
    'hover:border-orange-500/40 shadow-sm shadow-orange-500/5',
  ghostButton: 'hover:text-orange-400 hover:border-orange-500/40',
  quickActive: 'bg-orange-500/20 text-orange-400 border border-orange-500/30',
  hintBox: 'text-orange-400/80 bg-orange-500/10',
}

const SELECTEL_ACCENT = {
  iconBg: 'bg-sky-500/20',
  icon: 'text-sky-400',
  badge: 'bg-sky-500/15 text-sky-400',
  primaryButton:
    'bg-gradient-to-r from-sky-500/20 to-cyan-500/20 text-sky-400 ' +
    'hover:from-sky-500/30 hover:to-cyan-500/30 border border-sky-500/20 ' +
    'hover:border-sky-500/40 shadow-sm shadow-sky-500/5',
  ghostButton: 'hover:text-sky-400 hover:border-sky-500/40',
  quickActive: 'bg-sky-500/20 text-sky-400 border border-sky-500/30',
  hintBox: 'text-sky-400/80 bg-sky-500/10',
}

const TIMEWEB_ACCENT = {
  iconBg: 'bg-indigo-500/20',
  icon: 'text-indigo-400',
  badge: 'bg-indigo-500/15 text-indigo-400',
  primaryButton:
    'bg-gradient-to-r from-indigo-500/20 to-violet-500/20 text-indigo-400 ' +
    'hover:from-indigo-500/30 hover:to-violet-500/30 border border-indigo-500/20 ' +
    'hover:border-indigo-500/40 shadow-sm shadow-indigo-500/5',
  ghostButton: 'hover:text-indigo-400 hover:border-indigo-500/40',
  quickActive: 'bg-indigo-500/20 text-indigo-400 border border-indigo-500/30',
  hintBox: 'text-indigo-400/80 bg-indigo-500/10',
}

export const PROVIDERS: Record<CloudProviderId, CloudProviderMeta> = {
  yandex_cloud: {
    id: 'yandex_cloud',
    nameKey: 'billing.provider_yandex_cloud',
    defaultCurrency: 'RUB',
    accent: YANDEX_ACCENT,
    fields: [
      {
        key: 'cloud_account_id',
        labelKey: 'billing.cloud_account_id',
        hintKey: 'billing.cloud_account_id_hint',
        placeholder: 'dn2xxxxxx',
        link: { url: 'https://console.yandex.cloud/billing/accounts', label: 'console.yandex.cloud' },
      },
      {
        key: 'cloud_credential',
        labelKey: 'billing.yc_token',
        hintKey: 'billing.yc_token_hint',
        placeholder: 'y0__xCr5em...',
        secret: true,
        link: {
          url: 'https://oauth.yandex.ru/authorize?response_type=token&client_id=1a6990aa636648e9b2ef855fa7bec2fb',
          labelKey: 'billing.yc_get_token_link',
        },
      },
    ],
  },
  selectel: {
    id: 'selectel',
    nameKey: 'billing.provider_selectel',
    defaultCurrency: 'RUB',
    accent: SELECTEL_ACCENT,
    fields: [
      {
        key: 'cloud_credential',
        labelKey: 'billing.selectel_token',
        hintKey: 'billing.selectel_token_hint',
        placeholder: 'xxxxxxxxxxxxxxxx',
        secret: true,
        link: {
          url: 'https://my.selectel.ru/profile/access/api-keys',
          labelKey: 'billing.selectel_get_token_link',
        },
      },
    ],
  },
  timeweb: {
    id: 'timeweb',
    nameKey: 'billing.provider_timeweb',
    defaultCurrency: 'RUB',
    accent: TIMEWEB_ACCENT,
    fields: [
      {
        key: 'cloud_credential',
        labelKey: 'billing.timeweb_token',
        hintKey: 'billing.timeweb_token_hint',
        placeholder: 'eyJhbGciOi...',
        secret: true,
        link: {
          url: 'https://timeweb.cloud/my/api-keys',
          labelKey: 'billing.timeweb_get_token_link',
        },
      },
    ],
  },
}

export const PROVIDER_IDS = Object.keys(PROVIDERS) as CloudProviderId[]

export function getProvider(id: string | null | undefined): CloudProviderMeta | null {
  if (!id) return null
  return PROVIDERS[id as CloudProviderId] ?? null
}
