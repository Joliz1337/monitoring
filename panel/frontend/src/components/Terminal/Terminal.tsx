import { useState, useRef, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import {
  Terminal as TerminalIcon,
  Play,
  Trash2,
  ChevronDown,
  Clock,
  CheckCircle2,
  XCircle,
  Loader2,
  History,
  X
} from 'lucide-react'
import { proxyApi, SSEStdoutEvent, SSEStderrEvent, SSEDoneEvent, SSEErrorEvent } from '../../api/client'

interface TerminalProps {
  serverId: number
}

interface OutputLine {
  type: 'stdout' | 'stderr' | 'info' | 'error'
  content: string
  timestamp: Date
}

interface CommandHistory {
  command: string
  timestamp: Date
}

const TIMEOUT_OPTIONS = [
  { value: 30, label: '30s' },
  { value: 60, label: '1m' },
  { value: 120, label: '2m' },
  { value: 300, label: '5m' },
  { value: 600, label: '10m' },
]

const SHELL_OPTIONS = [
  { value: 'sh', label: 'sh' },
  { value: 'bash', label: 'bash' },
]

const HISTORY_KEY = 'terminal_history'
const MAX_HISTORY = 50

export default function Terminal({ serverId }: TerminalProps) {
  const { t } = useTranslation()
  const [isExpanded, setIsExpanded] = useState(false)
  const [command, setCommand] = useState('')
  const [timeout, setTimeout] = useState(30)
  const [shell, setShell] = useState<'sh' | 'bash'>('sh')
  const [output, setOutput] = useState<OutputLine[]>([])
  const [isRunning, setIsRunning] = useState(false)
  const [lastResult, setLastResult] = useState<{ success: boolean; exitCode: number; time: number } | null>(null)
  const [history, setHistory] = useState<CommandHistory[]>([])
  const [showHistory, setShowHistory] = useState(false)
  const [historyIndex, setHistoryIndex] = useState(-1)
  
  const outputRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const abortControllerRef = useRef<AbortController | null>(null)

  // Load history from localStorage
  useEffect(() => {
    const saved = localStorage.getItem(`${HISTORY_KEY}_${serverId}`)
    if (saved) {
      try {
        const parsed = JSON.parse(saved)
        setHistory(parsed.map((h: { command: string; timestamp: string }) => ({
          command: h.command,
          timestamp: new Date(h.timestamp)
        })))
      } catch {
        // ignore parse errors
      }
    }
  }, [serverId])

  // Save history to localStorage
  const saveHistory = useCallback((newHistory: CommandHistory[]) => {
    localStorage.setItem(`${HISTORY_KEY}_${serverId}`, JSON.stringify(newHistory))
  }, [serverId])

  // Auto-scroll output
  useEffect(() => {
    if (outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight
    }
  }, [output])

  const addToHistory = useCallback((cmd: string) => {
    const newEntry = { command: cmd, timestamp: new Date() }
    setHistory(prev => {
      // Remove duplicate if exists
      const filtered = prev.filter(h => h.command !== cmd)
      const updated = [newEntry, ...filtered].slice(0, MAX_HISTORY)
      saveHistory(updated)
      return updated
    })
    setHistoryIndex(-1)
  }, [saveHistory])

  const executeCommand = useCallback(async () => {
    if (!command.trim() || isRunning) return

    const trimmedCommand = command.trim()
    addToHistory(trimmedCommand)
    
    setIsRunning(true)
    setLastResult(null)
    setOutput(prev => [
      ...prev,
      { type: 'info', content: `$ ${trimmedCommand}`, timestamp: new Date() }
    ])

    abortControllerRef.current = new AbortController()

    try {
      const url = proxyApi.getExecuteStreamUrl(serverId)
      
      const response = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          command: trimmedCommand,
          timeout,
          shell
        }),
        credentials: 'include',
        signal: abortControllerRef.current.signal
      })

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`)
      }

      const reader = response.body?.getReader()
      if (!reader) {
        throw new Error('No response body')
      }

      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        
        // Parse SSE events
        const lines = buffer.split('\n')
        buffer = ''
        
        let currentEvent = ''
        let currentData = ''
        
        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEvent = line.slice(7)
          } else if (line.startsWith('data: ')) {
            currentData = line.slice(6)
          } else if (line === '' && currentEvent && currentData) {
            // Process event
            try {
              const data = JSON.parse(currentData)
              
              switch (currentEvent) {
                case 'stdout': {
                  const stdoutData = data as SSEStdoutEvent
                  setOutput(prev => [
                    ...prev,
                    { type: 'stdout', content: stdoutData.line, timestamp: new Date() }
                  ])
                  break
                }
                case 'stderr': {
                  const stderrData = data as SSEStderrEvent
                  setOutput(prev => [
                    ...prev,
                    { type: 'stderr', content: stderrData.line, timestamp: new Date() }
                  ])
                  break
                }
                case 'done': {
                  const doneData = data as SSEDoneEvent
                  setLastResult({
                    success: doneData.success,
                    exitCode: doneData.exit_code,
                    time: doneData.execution_time_ms
                  })
                  setIsRunning(false)
                  break
                }
                case 'error': {
                  const errorData = data as SSEErrorEvent
                  setOutput(prev => [
                    ...prev,
                    { type: 'error', content: errorData.message, timestamp: new Date() }
                  ])
                  break
                }
              }
            } catch {
              // ignore parse errors
            }
            
            currentEvent = ''
            currentData = ''
          } else if (line !== '') {
            // Incomplete line, add to buffer
            buffer = line
          }
        }
      }
    } catch (error) {
      if ((error as Error).name === 'AbortError') {
        setOutput(prev => [
          ...prev,
          { type: 'info', content: t('terminal.command_cancelled'), timestamp: new Date() }
        ])
      } else {
        setOutput(prev => [
          ...prev,
          { type: 'error', content: `${t('terminal.connection_error')}: ${(error as Error).message}`, timestamp: new Date() }
        ])
      }
      setLastResult({ success: false, exitCode: -1, time: 0 })
    } finally {
      setIsRunning(false)
      abortControllerRef.current = null
    }
  }, [command, timeout, shell, serverId, isRunning, addToHistory, t])

  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      executeCommand()
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      if (history.length > 0) {
        const newIndex = Math.min(historyIndex + 1, history.length - 1)
        setHistoryIndex(newIndex)
        setCommand(history[newIndex].command)
      }
    } else if (e.key === 'ArrowDown') {
      e.preventDefault()
      if (historyIndex > 0) {
        const newIndex = historyIndex - 1
        setHistoryIndex(newIndex)
        setCommand(history[newIndex].command)
      } else if (historyIndex === 0) {
        setHistoryIndex(-1)
        setCommand('')
      }
    } else if (e.key === 'c' && e.ctrlKey && isRunning) {
      e.preventDefault()
      abortControllerRef.current?.abort()
    }
  }, [executeCommand, history, historyIndex, isRunning])

  const clearOutput = useCallback(() => {
    setOutput([])
    setLastResult(null)
  }, [])

  const clearHistory = useCallback(() => {
    setHistory([])
    localStorage.removeItem(`${HISTORY_KEY}_${serverId}`)
  }, [serverId])

  const selectFromHistory = useCallback((cmd: string) => {
    setCommand(cmd)
    setShowHistory(false)
    inputRef.current?.focus()
  }, [])

  return (
    <motion.div 
      className="card"
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
    >
      {/* Header */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center justify-between text-left"
      >
        <div className="flex items-center gap-3">
          <TerminalIcon className="w-5 h-5 text-accent-500" />
          <span className="font-semibold text-dark-100">{t('terminal.title')}</span>
          {lastResult && (
            <motion.div
              initial={{ opacity: 0, scale: 0.8 }}
              animate={{ opacity: 1, scale: 1 }}
              className={`flex items-center gap-1.5 px-2 py-0.5 rounded-lg text-xs ${
                lastResult.success 
                  ? 'bg-success/10 text-success' 
                  : 'bg-danger/10 text-danger'
              }`}
            >
              {lastResult.success ? (
                <CheckCircle2 className="w-3 h-3" />
              ) : (
                <XCircle className="w-3 h-3" />
              )}
              <span>exit {lastResult.exitCode}</span>
              <span className="text-dark-500">({lastResult.time}ms)</span>
            </motion.div>
          )}
        </div>
        <motion.div
          animate={{ rotate: isExpanded ? 180 : 0 }}
          transition={{ duration: 0.2 }}
        >
          <ChevronDown className="w-5 h-5 text-dark-400" />
        </motion.div>
      </button>

      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="mt-4 space-y-4">
              {/* Command input area */}
              <div className="flex flex-col gap-3">
                <div className="flex gap-2">
                  {/* Command input */}
                  <div className="flex-1 relative">
                    <input
                      ref={inputRef}
                      type="text"
                      value={command}
                      onChange={(e) => setCommand(e.target.value)}
                      onKeyDown={handleKeyDown}
                      placeholder={t('terminal.placeholder')}
                      disabled={isRunning}
                      className="input w-full font-mono text-sm pr-10"
                    />
                    {history.length > 0 && (
                      <button
                        onClick={() => setShowHistory(!showHistory)}
                        className="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 hover:bg-dark-700 rounded-lg text-dark-400 hover:text-dark-200 transition-colors"
                        title={t('terminal.history')}
                      >
                        <History className="w-4 h-4" />
                      </button>
                    )}
                  </div>

                  {/* Execute button */}
                  <motion.button
                    onClick={executeCommand}
                    disabled={!command.trim() || isRunning}
                    className="btn btn-primary px-4"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    {isRunning ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <Play className="w-4 h-4" />
                    )}
                    <span className="hidden sm:inline">
                      {isRunning ? t('terminal.running') : t('terminal.execute')}
                    </span>
                  </motion.button>
                </div>

                {/* History dropdown */}
                <AnimatePresence>
                  {showHistory && history.length > 0 && (
                    <motion.div
                      initial={{ opacity: 0, y: -10 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, y: -10 }}
                      className="bg-dark-800 border border-dark-700 rounded-xl overflow-hidden"
                    >
                      <div className="flex items-center justify-between px-3 py-2 border-b border-dark-700">
                        <span className="text-xs text-dark-400">{t('terminal.history')}</span>
                        <button
                          onClick={clearHistory}
                          className="text-xs text-dark-500 hover:text-danger transition-colors"
                        >
                          {t('terminal.clear_history')}
                        </button>
                      </div>
                      <div className="max-h-40 overflow-y-auto">
                        {history.slice(0, 20).map((h, i) => (
                          <button
                            key={i}
                            onClick={() => selectFromHistory(h.command)}
                            className="w-full px-3 py-2 text-left text-sm font-mono text-dark-200 hover:bg-dark-700 transition-colors truncate"
                          >
                            {h.command}
                          </button>
                        ))}
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>

                {/* Options row */}
                <div className="flex flex-wrap items-center gap-3">
                  {/* Timeout selector */}
                  <div className="flex items-center gap-2">
                    <Clock className="w-4 h-4 text-dark-400" />
                    <select
                      value={timeout}
                      onChange={(e) => setTimeout(Number(e.target.value))}
                      disabled={isRunning}
                      className="input py-1.5 px-2 text-xs w-20"
                    >
                      {TIMEOUT_OPTIONS.map(opt => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  </div>

                  {/* Shell selector */}
                  <div className="flex items-center gap-2">
                    <TerminalIcon className="w-4 h-4 text-dark-400" />
                    <select
                      value={shell}
                      onChange={(e) => setShell(e.target.value as 'sh' | 'bash')}
                      disabled={isRunning}
                      className="input py-1.5 px-2 text-xs w-20"
                    >
                      {SHELL_OPTIONS.map(opt => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  </div>

                  {/* Clear output */}
                  {output.length > 0 && (
                    <button
                      onClick={clearOutput}
                      className="flex items-center gap-1.5 text-xs text-dark-400 hover:text-dark-200 transition-colors"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                      {t('terminal.clear')}
                    </button>
                  )}

                  {/* Cancel running command */}
                  {isRunning && (
                    <button
                      onClick={() => abortControllerRef.current?.abort()}
                      className="flex items-center gap-1.5 text-xs text-danger hover:text-danger/80 transition-colors"
                    >
                      <X className="w-3.5 h-3.5" />
                      {t('terminal.cancel')}
                    </button>
                  )}
                </div>
              </div>

              {/* Output area */}
              {output.length > 0 && (
                <div
                  ref={outputRef}
                  className="bg-dark-950 rounded-xl p-4 font-mono text-sm max-h-96 overflow-y-auto border border-dark-800"
                >
                  {output.map((line, i) => (
                    <div
                      key={i}
                      className={`whitespace-pre-wrap break-all ${
                        line.type === 'stdout' ? 'text-success' :
                        line.type === 'stderr' ? 'text-danger' :
                        line.type === 'error' ? 'text-danger font-bold' :
                        'text-accent-400'
                      }`}
                    >
                      {line.content}
                    </div>
                  ))}
                  {isRunning && (
                    <motion.span
                      className="inline-block w-2 h-4 bg-accent-500"
                      animate={{ opacity: [1, 0, 1] }}
                      transition={{ duration: 1, repeat: Infinity }}
                    />
                  )}
                </div>
              )}

              {/* Empty state */}
              {output.length === 0 && (
                <div className="text-center py-8 text-dark-500 text-sm">
                  {t('terminal.empty_hint')}
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}
