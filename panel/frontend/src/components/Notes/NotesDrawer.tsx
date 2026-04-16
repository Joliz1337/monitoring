import { useState, useRef, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, Wifi, WifiOff, Loader2, Plus, Trash2, FileText, ListTodo, Check } from 'lucide-react'
import { useNotesStore } from '../../stores/notesStore'
import { useTranslation } from 'react-i18next'

type Tab = 'notes' | 'tasks'

export default function NotesDrawer() {
  const { t } = useTranslation()
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const taskInputRef = useRef<HTMLInputElement>(null)
  const [tab, setTab] = useState<Tab>('notes')
  const [newTask, setNewTask] = useState('')

  const {
    content, isOpen, isLoading, isSaving, isConnected, version,
    tasks, close, setContent, createTask, toggleTask, deleteTask,
  } = useNotesStore()

  useEffect(() => {
    if (isOpen && tab === 'notes' && textareaRef.current) {
      const timer = setTimeout(() => textareaRef.current?.focus(), 300)
      return () => clearTimeout(timer)
    }
  }, [isOpen, tab])

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) close()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [isOpen, close])

  const handleAddTask = async () => {
    const text = newTask.trim()
    if (!text) return
    await createTask(text)
    setNewTask('')
    taskInputRef.current?.focus()
  }

  const doneTasks = tasks.filter(t => t.is_done)
  const activeTasks = tasks.filter(t => !t.is_done)

  return (
    <AnimatePresence>
      {isOpen && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="fixed inset-0 bg-black/40 backdrop-blur-sm z-[60]"
            onClick={close}
          />

          <motion.div
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={{ type: 'spring', damping: 25, stiffness: 300 }}
            className="fixed right-0 top-0 bottom-0 z-[60] w-full max-w-md
                       bg-dark-900/95 backdrop-blur-xl border-l border-dark-800/50
                       flex flex-col shadow-2xl"
          >
            {/* Header */}
            <div className="flex items-center justify-between p-4 border-b border-dark-800/50">
              <div className="flex items-center gap-3">
                <h2 className="text-lg font-semibold text-dark-100">{t('notes.title')}</h2>
                {isConnected
                  ? <Wifi className="w-3.5 h-3.5 text-success" />
                  : <WifiOff className="w-3.5 h-3.5 text-dark-500" />}
                {isSaving && (
                  <div className="flex items-center gap-1 text-xs text-dark-400">
                    <Loader2 className="w-3 h-3 animate-spin" />
                  </div>
                )}
              </div>
              <motion.button
                onClick={close}
                className="p-2 rounded-lg hover:bg-dark-800 text-dark-400 hover:text-dark-200 transition-colors"
                whileHover={{ scale: 1.1 }}
                whileTap={{ scale: 0.9 }}
              >
                <X className="w-5 h-5" />
              </motion.button>
            </div>

            {/* Tabs */}
            <div className="flex border-b border-dark-800/50">
              <button
                onClick={() => setTab('notes')}
                className={`flex-1 flex items-center justify-center gap-2 py-2.5 text-sm font-medium transition-colors
                  ${tab === 'notes' ? 'text-accent-400 border-b-2 border-accent-400' : 'text-dark-400 hover:text-dark-200'}`}
              >
                <FileText className="w-4 h-4" />
                {t('notes.tab_notes')}
              </button>
              <button
                onClick={() => setTab('tasks')}
                className={`flex-1 flex items-center justify-center gap-2 py-2.5 text-sm font-medium transition-colors
                  ${tab === 'tasks' ? 'text-accent-400 border-b-2 border-accent-400' : 'text-dark-400 hover:text-dark-200'}`}
              >
                <ListTodo className="w-4 h-4" />
                {t('notes.tab_tasks')}
                {activeTasks.length > 0 && (
                  <span className="text-xs bg-accent-500/20 text-accent-400 px-1.5 py-0.5 rounded-full">{activeTasks.length}</span>
                )}
              </button>
            </div>

            {/* Content */}
            <div className="flex-1 overflow-hidden flex flex-col">
              {isLoading ? (
                <div className="flex items-center justify-center h-full">
                  <Loader2 className="w-6 h-6 text-accent-400 animate-spin" />
                </div>
              ) : tab === 'notes' ? (
                <div className="flex-1 p-4 overflow-hidden">
                  <textarea
                    ref={textareaRef}
                    value={content}
                    onChange={e => setContent(e.target.value)}
                    placeholder={t('notes.placeholder')}
                    className="w-full h-full resize-none bg-dark-800/50 border border-dark-700/50
                              rounded-xl p-4 text-dark-200 placeholder-dark-500
                              focus:outline-none focus:border-accent-500/30 focus:ring-1
                              focus:ring-accent-500/20 transition-all
                              font-mono text-sm leading-relaxed"
                    spellCheck={false}
                  />
                </div>
              ) : (
                <div className="flex-1 flex flex-col overflow-hidden">
                  {/* Add task input */}
                  <div className="p-3 border-b border-dark-800/50">
                    <div className="flex items-center gap-2">
                      <input
                        ref={taskInputRef}
                        value={newTask}
                        onChange={e => setNewTask(e.target.value)}
                        onKeyDown={e => { if (e.key === 'Enter') handleAddTask() }}
                        placeholder={t('notes.task_placeholder')}
                        className="flex-1 bg-dark-800/50 border border-dark-700/50 rounded-lg px-3 py-2
                                  text-sm text-dark-200 placeholder-dark-500
                                  focus:outline-none focus:border-accent-500/30 transition-all"
                      />
                      <motion.button
                        onClick={handleAddTask}
                        className="p-2 rounded-lg bg-accent-500/20 text-accent-400 hover:bg-accent-500/30 transition-colors"
                        whileTap={{ scale: 0.9 }}
                      >
                        <Plus className="w-4 h-4" />
                      </motion.button>
                    </div>
                  </div>

                  {/* Tasks list */}
                  <div className="flex-1 overflow-y-auto p-3 space-y-1">
                    <AnimatePresence mode="popLayout">
                      {activeTasks.map(task => (
                        <motion.div
                          key={task.id}
                          layout
                          initial={{ opacity: 0, x: -10 }}
                          animate={{ opacity: 1, x: 0 }}
                          exit={{ opacity: 0, x: 10 }}
                          className="group flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-dark-800/50 transition-colors"
                        >
                          <button
                            onClick={() => toggleTask(task.id, true)}
                            className="w-4.5 h-4.5 rounded border border-dark-500 hover:border-accent-400 shrink-0 transition-colors flex items-center justify-center"
                          />
                          <span className="flex-1 text-sm text-dark-200">{task.text}</span>
                          <button
                            onClick={() => deleteTask(task.id)}
                            className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-dark-700 text-dark-500 hover:text-danger transition-all"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </motion.div>
                      ))}
                    </AnimatePresence>

                    {/* Done tasks */}
                    {doneTasks.length > 0 && (
                      <div className="pt-3 mt-2 border-t border-dark-800/30">
                        <span className="text-xs text-dark-500 px-3">{t('notes.done')} ({doneTasks.length})</span>
                        <AnimatePresence mode="popLayout">
                          {doneTasks.map(task => (
                            <motion.div
                              key={task.id}
                              layout
                              initial={{ opacity: 0 }}
                              animate={{ opacity: 1 }}
                              exit={{ opacity: 0 }}
                              className="group flex items-center gap-2 px-3 py-1.5 rounded-lg hover:bg-dark-800/30 transition-colors"
                            >
                              <button
                                onClick={() => toggleTask(task.id, false)}
                                className="w-4.5 h-4.5 rounded bg-accent-500/20 border border-accent-500/40 shrink-0 flex items-center justify-center"
                              >
                                <Check className="w-3 h-3 text-accent-400" />
                              </button>
                              <span className="flex-1 text-sm text-dark-500 line-through">{task.text}</span>
                              <button
                                onClick={() => deleteTask(task.id)}
                                className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-dark-700 text-dark-500 hover:text-danger transition-all"
                              >
                                <Trash2 className="w-3.5 h-3.5" />
                              </button>
                            </motion.div>
                          ))}
                        </AnimatePresence>
                      </div>
                    )}

                    {tasks.length === 0 && (
                      <div className="text-sm text-dark-500 text-center py-8">{t('notes.no_tasks')}</div>
                    )}
                  </div>
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="px-4 py-2 border-t border-dark-800/50 flex items-center justify-between text-xs text-dark-500">
              <span>{t('notes.shared_hint')}</span>
              {tab === 'notes' && <span>v{version}</span>}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  )
}
