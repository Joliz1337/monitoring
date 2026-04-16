import type { FAQLang, FAQScreen } from '../../components/FAQ/faq.types'

const ruFiles = import.meta.glob('./content/ru/*.md', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

const enFiles = import.meta.glob('./content/en/*.md', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

const buildIndex = (files: Record<string, string>): Record<string, string> => {
  const index: Record<string, string> = {}
  for (const [path, content] of Object.entries(files)) {
    const name = path.split('/').pop()?.replace(/\.md$/, '')
    if (name) index[name] = content
  }
  return index
}

const ruIndex = buildIndex(ruFiles)
const enIndex = buildIndex(enFiles)

export const getFAQContent = (screen: FAQScreen, lang: FAQLang): string | null => {
  const primary = lang === 'ru' ? ruIndex[screen] : enIndex[screen]
  if (primary) return primary
  return ruIndex[screen] ?? enIndex[screen] ?? null
}
