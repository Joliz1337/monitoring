import { Terminal, KeyRound, Save, Loader2, Plus, X, Network, Shield } from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import type { RemnawaveCertProfile, HAProxyConfigProfile, FirewallProfile } from '../../api/client'

export interface DeployFormData {
  enabled: boolean
  sshPort: string
  sshUser: string
  sshAuth: 'password' | 'key'
  sshPassword: string
  sshPrivateKey: string
  sshPassphrase: string
  sshPreset: 'none' | 'recommended' | 'maximum'
  changePassword: boolean
  newPassword: string
  installWarp: boolean
  installOptimizations: boolean
  optProfile: 'vpn' | 'panel'
  optNicMode: 'auto' | 'multiqueue' | 'hybrid' | 'rps'
  installRemnawave: boolean
  remnaCertMode: 'inline' | 'saved'
  remnaCertInline: string
  remnaCertProfileId: number | null
  installProxy: boolean
  proxyUrl: string
  haproxyProfileId: number | null
  firewallProfileId: number | null
}

export const DEPLOY_DEFAULTS: DeployFormData = {
  enabled: false,
  sshPort: '22',
  sshUser: 'root',
  sshAuth: 'password',
  sshPassword: '',
  sshPrivateKey: '',
  sshPassphrase: '',
  sshPreset: 'none',
  changePassword: false,
  newPassword: '',
  installWarp: false,
  installOptimizations: false,
  optProfile: 'vpn',
  optNicMode: 'auto',
  installRemnawave: false,
  remnaCertMode: 'inline',
  remnaCertInline: '',
  remnaCertProfileId: null,
  installProxy: false,
  proxyUrl: '',
  haproxyProfileId: null,
  firewallProfileId: null,
}

interface Props {
  deploy: DeployFormData
  onChange: (patch: Partial<DeployFormData>) => void
  remnaCertProfiles: RemnawaveCertProfile[]
  haproxyProfiles: HAProxyConfigProfile[]
  firewallProfiles: FirewallProfile[]
  savingCert: boolean
  onSaveCert: () => void
  onDeleteCert: (id: number) => void
  footerSlot?: React.ReactNode
}

export default function DeployTargetFields({
  deploy,
  onChange,
  remnaCertProfiles,
  haproxyProfiles,
  firewallProfiles,
  savingCert,
  onSaveCert,
  onDeleteCert,
  footerSlot,
}: Props) {
  const { t } = useTranslation()

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-3 gap-3">
        <div>
          <label className="block text-xs text-dark-400 mb-1.5">{t('servers.deploy_ssh_port')}</label>
          <input
            type="text"
            value={deploy.sshPort}
            onChange={(e) => onChange({ sshPort: e.target.value.replace(/\D/g, '') })}
            placeholder="22"
            className="input text-center"
          />
        </div>
        <div className="col-span-2">
          <label className="block text-xs text-dark-400 mb-1.5">{t('servers.deploy_ssh_user')}</label>
          <input
            type="text"
            value={deploy.sshUser}
            onChange={(e) => onChange({ sshUser: e.target.value })}
            placeholder="root"
            className="input"
            autoComplete="off"
          />
        </div>
      </div>

      <div>
        <label className="block text-xs text-dark-400 mb-1.5">{t('servers.deploy_ssh_auth')}</label>
        <div className="flex gap-2 mb-2">
          {(['password', 'key'] as const).map(m => (
            <button
              key={m}
              type="button"
              onClick={() => onChange({ sshAuth: m })}
              className={`btn text-sm flex-1 ${deploy.sshAuth === m ? 'btn-primary' : 'btn-secondary'}`}
            >
              {m === 'password' ? <KeyRound className="w-4 h-4" /> : <Terminal className="w-4 h-4" />}
              {t(m === 'password' ? 'servers.deploy_auth_password' : 'servers.deploy_auth_key')}
            </button>
          ))}
        </div>
        {deploy.sshAuth === 'password' ? (
          <input
            type="password"
            value={deploy.sshPassword}
            onChange={(e) => onChange({ sshPassword: e.target.value })}
            placeholder={t('servers.deploy_ssh_password')}
            className="input"
            autoComplete="new-password"
          />
        ) : (
          <div className="space-y-2">
            <textarea
              value={deploy.sshPrivateKey}
              onChange={(e) => onChange({ sshPrivateKey: e.target.value })}
              placeholder={t('servers.deploy_ssh_key_placeholder')}
              className="input font-mono text-xs resize-none w-full min-h-[88px]"
            />
            <input
              type="password"
              value={deploy.sshPassphrase}
              onChange={(e) => onChange({ sshPassphrase: e.target.value })}
              placeholder={t('servers.deploy_ssh_passphrase')}
              className="input"
              autoComplete="new-password"
            />
          </div>
        )}
      </div>

      <div className="space-y-3 pt-1">
        <p className="text-xs text-dark-400">{t('servers.deploy_ssh_hardening')}</p>

        <div>
          <label className="block text-xs text-dark-400 mb-1.5">{t('servers.deploy_ssh_preset')}</label>
          <div className="flex gap-2">
            {(['none', 'recommended', 'maximum'] as const).map(p => (
              <button
                key={p}
                type="button"
                onClick={() => onChange({ sshPreset: p })}
                className={`btn text-xs flex-1 ${deploy.sshPreset === p ? 'btn-primary' : 'btn-secondary'}`}
              >
                {t(`servers.deploy_preset_${p}`)}
              </button>
            ))}
          </div>
        </div>

        <label className="flex items-center gap-2.5 cursor-pointer">
          <input
            type="checkbox"
            checked={deploy.changePassword}
            onChange={(e) => onChange({ changePassword: e.target.checked })}
            className="w-4 h-4 rounded accent-accent-500 cursor-pointer"
          />
          <span className="text-sm text-dark-200">{t('servers.deploy_change_password')}</span>
        </label>
        {deploy.changePassword && (
          <div className="ml-6">
            <input
              type="password"
              value={deploy.newPassword}
              onChange={(e) => onChange({ newPassword: e.target.value })}
              placeholder={t('servers.deploy_new_password')}
              className="input"
              autoComplete="new-password"
            />
          </div>
        )}
      </div>

      <div className="space-y-3 pt-1">
        <p className="text-xs text-dark-400">{t('servers.deploy_extras')}</p>

        <label className="flex items-center gap-2.5 cursor-pointer">
          <input
            type="checkbox"
            checked={deploy.installWarp}
            onChange={(e) => onChange({ installWarp: e.target.checked })}
            className="w-4 h-4 rounded accent-accent-500 cursor-pointer"
          />
          <span className="text-sm text-dark-200">{t('servers.deploy_install_warp')}</span>
        </label>

        <label className="flex items-start gap-2.5 cursor-pointer">
          <input
            type="checkbox"
            checked={deploy.installOptimizations}
            onChange={(e) => onChange({ installOptimizations: e.target.checked })}
            className="w-4 h-4 mt-0.5 rounded accent-accent-500 cursor-pointer"
          />
          <span className="text-sm text-dark-200">
            {t('servers.deploy_install_optimizations')}
            <span className="block text-xs text-dark-500">{t('servers.deploy_optimizations_hint')}</span>
          </span>
        </label>
        {deploy.installOptimizations && (
          <div className="ml-6 space-y-2">
            <div>
              <label className="block text-xs text-dark-400 mb-1">{t('servers.deploy_opt_profile')}</label>
              <div className="flex gap-2">
                {(['vpn', 'panel'] as const).map(p => (
                  <button
                    key={p}
                    type="button"
                    onClick={() => onChange({ optProfile: p })}
                    className={`btn text-xs flex-1 ${deploy.optProfile === p ? 'btn-primary' : 'btn-secondary'}`}
                  >
                    {t(p === 'vpn' ? 'servers.deploy_opt_vpn' : 'servers.deploy_opt_universal')}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <label className="block text-xs text-dark-400 mb-1">{t('servers.deploy_opt_nic')}</label>
              <div className="flex gap-2">
                {(['auto', 'multiqueue', 'hybrid', 'rps'] as const).map(m => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => onChange({ optNicMode: m })}
                    className={`btn text-xs flex-1 ${deploy.optNicMode === m ? 'btn-primary' : 'btn-secondary'}`}
                  >
                    {t(`servers.deploy_nic_${m}`)}
                  </button>
                ))}
              </div>
            </div>
          </div>
        )}

        <label className="flex items-center gap-2.5 cursor-pointer">
          <input
            type="checkbox"
            checked={deploy.installRemnawave}
            onChange={(e) => onChange({
              installRemnawave: e.target.checked,
              remnaCertMode: e.target.checked && remnaCertProfiles.length > 0 ? 'saved' : 'inline',
            })}
            className="w-4 h-4 rounded accent-accent-500 cursor-pointer"
          />
          <span className="text-sm text-dark-200">{t('servers.deploy_install_remnawave')}</span>
        </label>
        <AnimatePresence>
          {deploy.installRemnawave && (
            <motion.div
              className="ml-6 space-y-2 overflow-hidden"
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.15 }}
            >
              <div className="flex flex-wrap gap-2">
                {remnaCertProfiles.map(p => {
                  const active = deploy.remnaCertMode === 'saved' && deploy.remnaCertProfileId === p.id
                  return (
                    <div
                      key={p.id}
                      className={`flex items-center rounded-lg border text-xs overflow-hidden ${
                        active
                          ? 'border-accent-500/50 bg-accent-500/10'
                          : 'border-dark-700/50 bg-dark-800/50'
                      }`}
                    >
                      <button
                        type="button"
                        onClick={() => onChange({
                          remnaCertMode: 'saved',
                          remnaCertProfileId: p.id,
                        })}
                        className={`px-2.5 py-1.5 transition-colors ${
                          active ? 'text-accent-300' : 'text-dark-200 hover:text-dark-50'
                        }`}
                      >
                        {p.name}
                      </button>
                      <button
                        type="button"
                        onClick={() => onDeleteCert(p.id)}
                        className="px-1.5 py-1.5 text-dark-500 hover:text-danger hover:bg-danger/10 transition-colors"
                        title={t('common.delete')}
                      >
                        <X className="w-3 h-3" />
                      </button>
                    </div>
                  )
                })}
                <button
                  type="button"
                  onClick={() => onChange({
                    remnaCertMode: 'inline',
                    remnaCertProfileId: null,
                  })}
                  className={`flex items-center gap-1 px-2.5 py-1.5 rounded-lg border text-xs transition-colors ${
                    deploy.remnaCertMode === 'inline'
                      ? 'border-accent-500/50 bg-accent-500/10 text-accent-300'
                      : 'border-dark-700/50 bg-dark-800/50 text-dark-200 hover:text-dark-50'
                  }`}
                >
                  <Plus className="w-3 h-3" />
                  {t('servers.deploy_remna_new')}
                </button>
              </div>
              {deploy.remnaCertMode === 'inline' && (
                <>
                  <textarea
                    value={deploy.remnaCertInline}
                    onChange={(e) => onChange({ remnaCertInline: e.target.value })}
                    placeholder={t('servers.deploy_remna_cert_placeholder')}
                    className="input font-mono text-xs resize-none w-full min-h-[72px]"
                  />
                  <button
                    type="button"
                    onClick={onSaveCert}
                    disabled={savingCert || !deploy.remnaCertInline.trim()}
                    className="btn btn-secondary text-xs"
                  >
                    {savingCert ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
                    {t('servers.deploy_remna_save')}
                  </button>
                </>
              )}
            </motion.div>
          )}
        </AnimatePresence>

        <label className="flex items-center gap-2.5 cursor-pointer">
          <input
            type="checkbox"
            checked={deploy.installProxy}
            onChange={(e) => onChange({ installProxy: e.target.checked })}
            className="w-4 h-4 rounded accent-accent-500 cursor-pointer"
          />
          <span className="text-sm text-dark-200">{t('servers.deploy_install_proxy')}</span>
        </label>
        {deploy.installProxy && (
          <div className="ml-6">
            <input
              type="text"
              value={deploy.proxyUrl}
              onChange={(e) => onChange({ proxyUrl: e.target.value })}
              placeholder={t('servers.deploy_proxy_placeholder')}
              className="input"
              autoComplete="off"
            />
            <p className="text-xs text-dark-500 mt-1">{t('servers.deploy_proxy_hint')}</p>
          </div>
        )}
      </div>

      <div className="space-y-3 pt-1">
        <p className="text-xs text-dark-400">{t('servers.deploy_bindings')}</p>

        <div>
          <label className="block text-xs text-dark-400 mb-1.5 flex items-center gap-1.5">
            <Network className="w-3.5 h-3.5" />
            {t('servers.deploy_haproxy_profile')}
          </label>
          <select
            value={deploy.haproxyProfileId ?? ''}
            onChange={(e) => onChange({ haproxyProfileId: e.target.value ? Number(e.target.value) : null })}
            className="input"
          >
            <option value="">{t('servers.deploy_profile_none')}</option>
            {haproxyProfiles.map(p => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-xs text-dark-400 mb-1.5 flex items-center gap-1.5">
            <Shield className="w-3.5 h-3.5" />
            {t('servers.deploy_firewall_profile')}
          </label>
          <select
            value={deploy.firewallProfileId ?? ''}
            onChange={(e) => onChange({ firewallProfileId: e.target.value ? Number(e.target.value) : null })}
            className="input"
          >
            <option value="">{t('servers.deploy_profile_none')}</option>
            {firewallProfiles.map(p => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </div>
      </div>

      {footerSlot}
    </div>
  )
}
