import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ChevronRight, Copy, Check, Download, Eye, EyeOff, Loader2, FileKey, AlertTriangle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { wildcardSSLApi, WildcardCertificateMaterial } from '../../api/client'

type MaterialField = 'fullchain_pem' | 'cert_pem' | 'chain_pem' | 'privkey_pem'

interface PemFile {
  field: MaterialField
  filename: string
  hintKey: string
  secret?: boolean
}

const PEM_FILES: PemFile[] = [
  { field: 'fullchain_pem', filename: 'fullchain.pem', hintKey: 'wildcard_ssl.pem_fullchain_hint' },
  { field: 'cert_pem', filename: 'cert.pem', hintKey: 'wildcard_ssl.pem_cert_hint' },
  { field: 'chain_pem', filename: 'chain.pem', hintKey: 'wildcard_ssl.pem_chain_hint' },
  { field: 'privkey_pem', filename: 'privkey.pem', hintKey: 'wildcard_ssl.pem_privkey_hint', secret: true },
]

async function copyText(value: string) {
  try {
    await navigator.clipboard.writeText(value)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = value
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
}

function downloadPem(content: string, filename: string) {
  const url = URL.createObjectURL(new Blob([content], { type: 'application/x-pem-file' }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

export default function CertificateMaterials({ certId }: { certId: number }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [material, setMaterial] = useState<WildcardCertificateMaterial | null>(null)
  const [copiedField, setCopiedField] = useState<MaterialField | null>(null)
  const [keyRevealed, setKeyRevealed] = useState(false)

  const handleToggle = async () => {
    if (open) {
      setOpen(false)
      setKeyRevealed(false)
      return
    }
    setOpen(true)
    if (material) return

    setLoading(true)
    try {
      const res = await wildcardSSLApi.getCertificatePem(certId)
      setMaterial(res.data)
    } catch {
      toast.error(t('wildcard_ssl.materials_error'))
      setOpen(false)
    } finally {
      setLoading(false)
    }
  }

  const handleCopy = async (field: MaterialField, content: string) => {
    await copyText(content)
    setCopiedField(field)
    setTimeout(() => setCopiedField(null), 2000)
  }

  return (
    <div className="rounded-xl border border-dark-700/50 bg-dark-800/30">
      <button
        onClick={handleToggle}
        className="w-full flex items-center gap-2 px-4 py-3 group"
      >
        <FileKey className="w-4 h-4 text-accent-400 shrink-0" />
        <span className="text-sm font-medium text-dark-200">{t('wildcard_ssl.materials_title')}</span>
        <span className="text-xs text-dark-500 hidden sm:inline truncate">{t('wildcard_ssl.materials_hint')}</span>
        <ChevronRight className={`w-4 h-4 ml-auto shrink-0 transition-all duration-200 ${
          open ? 'rotate-90 text-accent-400' : 'text-dark-600 group-hover:text-dark-400'
        }`} />
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            {loading || !material ? (
              <div className="flex items-center justify-center py-8">
                <Loader2 className="w-5 h-5 text-accent-400 animate-spin" />
              </div>
            ) : (
              <div className="px-4 pb-4 space-y-3 border-t border-dark-700/40 pt-3">
                {PEM_FILES.map(file => {
                  const content = material[file.field]
                  const hidden = file.secret && !keyRevealed
                  const filename = `${material.base_domain}-${file.filename}`

                  return (
                    <div key={file.field} className="space-y-1.5">
                      <div className="flex items-center justify-between gap-3 flex-wrap">
                        <div className="min-w-0">
                          <div className="text-xs font-mono text-dark-200">{file.filename}</div>
                          <div className="text-[11px] text-dark-500">{t(file.hintKey)}</div>
                        </div>
                        <div className="flex items-center gap-1.5 shrink-0">
                          {file.secret && (
                            <button
                              onClick={() => setKeyRevealed(prev => !prev)}
                              className="px-2 py-1 rounded-lg text-[11px] text-dark-400 hover:text-dark-200 hover:bg-dark-700/50 transition-colors flex items-center gap-1"
                            >
                              {keyRevealed ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                              {keyRevealed ? t('wildcard_ssl.pem_hide') : t('wildcard_ssl.pem_reveal')}
                            </button>
                          )}
                          <button
                            onClick={() => handleCopy(file.field, content)}
                            disabled={!content}
                            className="px-2 py-1 rounded-lg text-[11px] text-dark-400 hover:text-accent-400 hover:bg-dark-700/50 transition-colors disabled:opacity-40 disabled:hover:text-dark-400 flex items-center gap-1"
                          >
                            {copiedField === file.field
                              ? <Check className="w-3.5 h-3.5 text-green-400" />
                              : <Copy className="w-3.5 h-3.5" />}
                            {copiedField === file.field ? t('common.copied') : t('common.copy')}
                          </button>
                          <button
                            onClick={() => downloadPem(content, filename)}
                            disabled={!content}
                            className="px-2 py-1 rounded-lg text-[11px] text-dark-400 hover:text-accent-400 hover:bg-dark-700/50 transition-colors disabled:opacity-40 disabled:hover:text-dark-400 flex items-center gap-1"
                          >
                            <Download className="w-3.5 h-3.5" />
                            {t('wildcard_ssl.pem_download')}
                          </button>
                        </div>
                      </div>

                      {!content ? (
                        <p className="text-[11px] text-dark-600 bg-dark-900/50 rounded-lg px-2.5 py-2">
                          {t('wildcard_ssl.pem_empty')}
                        </p>
                      ) : (
                        <pre className={`text-[11px] leading-relaxed font-mono text-dark-400 bg-dark-900/60 rounded-lg px-2.5 py-2 max-h-40 overflow-auto whitespace-pre ${
                          hidden ? 'blur-sm select-none' : ''
                        }`}>
                          {content.trim()}
                        </pre>
                      )}
                    </div>
                  )
                })}

                <p className="text-[11px] text-yellow-300/80 flex items-start gap-1.5">
                  <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-px" />
                  {t('wildcard_ssl.pem_secret_warning')}
                </p>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
