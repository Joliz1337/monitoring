/**
 * Реестр облачных провайдеров биллинга.
 *
 * Карточка, формы и калькулятор читают провайдера отсюда — новый провайдер
 * добавляется записью в PROVIDERS, а не ветками по всему разделу.
 */
export type CloudProviderId = 'yandex_cloud' | 'selectel' | 'timeweb' | 'vk_cloud' | 'cloud_ru'

export interface CloudCredentialField {
  /** Поле модели, куда уходит значение */
  key: 'cloud_account_id' | 'cloud_credential' | 'cloud_login'
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

const VK_CLOUD_ACCENT = {
  iconBg: 'bg-blue-500/20',
  icon: 'text-blue-400',
  badge: 'bg-blue-500/15 text-blue-400',
  primaryButton:
    'bg-gradient-to-r from-blue-500/20 to-sky-500/20 text-blue-400 ' +
    'hover:from-blue-500/30 hover:to-sky-500/30 border border-blue-500/20 ' +
    'hover:border-blue-500/40 shadow-sm shadow-blue-500/5',
  ghostButton: 'hover:text-blue-400 hover:border-blue-500/40',
  quickActive: 'bg-blue-500/20 text-blue-400 border border-blue-500/30',
  hintBox: 'text-blue-400/80 bg-blue-500/10',
}

const CLOUD_RU_ACCENT = {
  iconBg: 'bg-emerald-500/20',
  icon: 'text-emerald-400',
  badge: 'bg-emerald-500/15 text-emerald-400',
  primaryButton:
    'bg-gradient-to-r from-emerald-500/20 to-teal-500/20 text-emerald-400 ' +
    'hover:from-emerald-500/30 hover:to-teal-500/30 border border-emerald-500/20 ' +
    'hover:border-emerald-500/40 shadow-sm shadow-emerald-500/5',
  ghostButton: 'hover:text-emerald-400 hover:border-emerald-500/40',
  quickActive: 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30',
  hintBox: 'text-emerald-400/80 bg-emerald-500/10',
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
        labelKey: 'billing.yc_key',
        hintKey: 'billing.yc_key_hint',
        placeholder: '{"id": "aje...", "service_account_id": "aje...", "private_key": "..."}',
        secret: true,
        link: {
          url: 'https://yandex.cloud/docs/iam/operations/authentication/manage-authorized-keys',
          labelKey: 'billing.yc_key_link',
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
  vk_cloud: {
    id: 'vk_cloud',
    nameKey: 'billing.provider_vk_cloud',
    defaultCurrency: 'RUB',
    accent: VK_CLOUD_ACCENT,
    fields: [
      {
        key: 'cloud_login',
        labelKey: 'billing.vk_login',
        hintKey: 'billing.vk_login_hint',
        placeholder: 'user@example.com',
      },
      {
        key: 'cloud_credential',
        labelKey: 'billing.vk_password',
        hintKey: 'billing.vk_password_hint',
        placeholder: '••••••••',
        secret: true,
      },
      {
        key: 'cloud_account_id',
        labelKey: 'billing.vk_project_id',
        hintKey: 'billing.vk_project_id_hint',
        placeholder: 'b5b7ffd4ef0547e5b222f445...',
        link: { url: 'https://msk.cloud.vk.ru/app/', label: 'msk.cloud.vk.ru' },
      },
    ],
  },
  cloud_ru: {
    id: 'cloud_ru',
    nameKey: 'billing.provider_cloud_ru',
    defaultCurrency: 'RUB',
    accent: CLOUD_RU_ACCENT,
    fields: [
      {
        key: 'cloud_login',
        labelKey: 'billing.cloudru_key_id',
        hintKey: 'billing.cloudru_key_id_hint',
        placeholder: '1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d',
        link: {
          url: 'https://cloud.ru/docs/console_api/ug/topics/guides__service_accounts_key-create',
          labelKey: 'billing.cloudru_key_link',
        },
      },
      {
        key: 'cloud_credential',
        labelKey: 'billing.cloudru_key_secret',
        hintKey: 'billing.cloudru_key_secret_hint',
        placeholder: '••••••••',
        secret: true,
      },
      {
        key: 'cloud_account_id',
        labelKey: 'billing.cloudru_agreement_id',
        hintKey: 'billing.cloudru_agreement_id_hint',
        placeholder: '3f2b8c1e-5a7d-4e9f-8b6a-1c2d3e4f5a6b',
        link: { url: 'https://console.cloud.ru/', label: 'console.cloud.ru' },
      },
    ],
  },
}

export const PROVIDER_IDS = Object.keys(PROVIDERS) as CloudProviderId[]

export function getProvider(id: string | null | undefined): CloudProviderMeta | null {
  if (!id) return null
  return PROVIDERS[id as CloudProviderId] ?? null
}
