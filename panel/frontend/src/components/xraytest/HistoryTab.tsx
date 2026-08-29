import { useCallback, useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { History, Loader2, Trash2, ChevronRight, ChevronDown } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { xrayTestApi, type XrayTestCell, type XrayTestRunSummary } from '../../api/client'
import { Section } from '../../pages/XrayTest'
import { ResultsTable } from './ResultsTable'
import { extractError } from './useTestRun'

export function HistoryTab() {
  const { t, i18n } = useTranslation()
  const [runs, setRuns] = useState<XrayTestRunSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [openRun, setOpenRun] = useState<number | null>(null)
  const [results, setResults] = useState<XrayTestCell[]>([])
  const [loadingResults, setLoadingResults] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const { data } = await xrayTestApi.history()
      setRuns(data.runs)
    } catch (error) {
      toast.error(extractError(error))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const toggle = async (runId: number) => {
    if (openRun === runId) {
      setOpenRun(null)
      setResults([])
      return
    }
    setOpenRun(runId)
    setLoadingResults(true)
    try {
      const { data } = await xrayTestApi.historyResults(runId)
      setResults(data.results)
    } catch (error) {
      toast.error(extractError(error))
      setResults([])
    } finally {
      setLoadingResults(false)
    }
  }

  const remove = async (runId: number) => {
    try {
      await xrayTestApi.deleteHistoryRun(runId)
      if (openRun === runId) { setOpenRun(null); setResults([]) }
      load()
    } catch (error) {
      toast.error(extractError(error))
    }
  }

  const formatDate = (value: string | null) => {
    if (!value) return '—'
    return new Date(value).toLocaleString(i18n.language === 'ru' ? 'ru-RU' : 'en-US')
  }

  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
      <Section title={t('xray_test.tab_history')} icon={<History className="w-5 h-5" />}>
        {loading ? (
          <div className="flex justify-center py-10">
            <Loader2 className="w-6 h-6 animate-spin text-dark-500" />
          </div>
        ) : runs.length === 0 ? (
          <p className="text-base text-dark-400 text-center py-8">{t('xray_test.no_history')}</p>
        ) : (
          <div className="space-y-2">
            {runs.map(run => (
              <div key={run.id} className="rounded-lg border border-dark-800/60 overflow-hidden">
                <div
                  className="flex items-center gap-3 p-3 cursor-pointer hover:bg-dark-800/30"
                  onClick={() => toggle(run.id)}
                >
                  {openRun === run.id
                    ? <ChevronDown className="w-5 h-5 text-dark-500" />
                    : <ChevronRight className="w-5 h-5 text-dark-500" />}
                  <div className="flex-1 min-w-0">
                    <div className="text-base text-dark-200">
                      {run.source_name || t(`xray_test.input_${run.source}`)}
                      <span className="text-dark-500 text-sm ml-2">
                        {run.location_name || t('xray_test.where_panel')}
                      </span>
                    </div>
                    <div className="text-[13px] text-dark-500">{formatDate(run.started_at)}</div>
                  </div>
                  <div className="flex items-center gap-2 text-sm shrink-0">
                    <span className="text-emerald-400">{run.ok}</span>
                    <span className="text-dark-600">/</span>
                    <span className="text-amber-400">{run.degraded}</span>
                    <span className="text-dark-600">/</span>
                    <span className="text-red-400">{run.fail}</span>
                  </div>
                  <button
                    className="text-dark-500 hover:text-red-400 shrink-0"
                    onClick={event => { event.stopPropagation(); remove(run.id) }}
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>

                {openRun === run.id && (
                  <div className="p-3 border-t border-dark-800/60 bg-dark-900/30">
                    {loadingResults ? (
                      <div className="flex justify-center py-6">
                        <Loader2 className="w-5 h-5 animate-spin text-dark-500" />
                      </div>
                    ) : (
                      <ResultsTable cells={results} groupBySni={false} />
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Section>
    </motion.div>
  )
}
