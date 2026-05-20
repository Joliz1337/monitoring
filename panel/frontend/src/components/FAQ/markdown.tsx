import { Fragment, type ReactNode } from 'react'

const escapeHtml = (s: string) => s.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c] ?? c))

const renderInline = (text: string): ReactNode[] => {
  const nodes: ReactNode[] = []
  let key = 0
  const pattern = /(\*\*([^*]+)\*\*)|(\*([^*]+)\*)|(`([^`]+)`)|(\[([^\]]+)\]\(([^)]+)\))/g
  let lastIndex = 0
  let match: RegExpExecArray | null
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index))
    }
    if (match[1]) {
      nodes.push(<strong key={key++} className="font-semibold text-dark-50">{match[2]}</strong>)
    } else if (match[3]) {
      nodes.push(<em key={key++} className="italic text-dark-200">{match[4]}</em>)
    } else if (match[5]) {
      nodes.push(
        <code key={key++} className="px-1.5 py-0.5 rounded bg-dark-800 border border-dark-700/50 text-accent-300 font-mono text-[0.85em]">
          {match[6]}
        </code>,
      )
    } else if (match[7]) {
      nodes.push(
        <a key={key++} href={match[9]} target="_blank" rel="noreferrer" className="text-accent-400 hover:text-accent-300 underline underline-offset-2">
          {match[8]}
        </a>,
      )
    }
    lastIndex = match.index + match[0].length
  }
  if (lastIndex < text.length) nodes.push(text.slice(lastIndex))
  return nodes
}

interface Block {
  type: 'h1' | 'h2' | 'h3' | 'h4' | 'p' | 'ul' | 'ol' | 'code' | 'hr' | 'blockquote'
  content?: string
  items?: string[]
  lang?: string
}

const parseMarkdown = (md: string): Block[] => {
  const lines = md.replace(/\r\n/g, '\n').split('\n')
  const blocks: Block[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]

    if (line.startsWith('```')) {
      const lang = line.slice(3).trim()
      const buf: string[] = []
      i++
      while (i < lines.length && !lines[i].startsWith('```')) {
        buf.push(lines[i])
        i++
      }
      i++
      blocks.push({ type: 'code', content: buf.join('\n'), lang })
      continue
    }

    if (/^#{1,4}\s/.test(line)) {
      const level = line.match(/^(#{1,4})\s/)![1].length as 1 | 2 | 3 | 4
      blocks.push({ type: `h${level}` as Block['type'], content: line.replace(/^#{1,4}\s/, '') })
      i++
      continue
    }

    if (/^\s*[-*]\s/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^\s*[-*]\s/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s/, ''))
        i++
      }
      blocks.push({ type: 'ul', items })
      continue
    }

    if (/^\s*\d+\.\s/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^\s*\d+\.\s/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s/, ''))
        i++
      }
      blocks.push({ type: 'ol', items })
      continue
    }

    if (line.startsWith('>')) {
      const buf: string[] = []
      while (i < lines.length && lines[i].startsWith('>')) {
        buf.push(lines[i].replace(/^>\s?/, ''))
        i++
      }
      blocks.push({ type: 'blockquote', content: buf.join(' ') })
      continue
    }

    if (/^---+$/.test(line.trim())) {
      blocks.push({ type: 'hr' })
      i++
      continue
    }

    if (line.trim() === '') {
      i++
      continue
    }

    const buf: string[] = [line]
    i++
    while (
      i < lines.length &&
      lines[i].trim() !== '' &&
      !/^#{1,4}\s/.test(lines[i]) &&
      !/^\s*[-*]\s/.test(lines[i]) &&
      !/^\s*\d+\.\s/.test(lines[i]) &&
      !lines[i].startsWith('```') &&
      !lines[i].startsWith('>')
    ) {
      buf.push(lines[i])
      i++
    }
    blocks.push({ type: 'p', content: buf.join(' ') })
  }

  return blocks
}

export const Markdown = ({ source }: { source: string }) => {
  const blocks = parseMarkdown(source)
  return (
    <div className="space-y-4 text-dark-200 leading-relaxed">
      {blocks.map((block, idx) => {
        switch (block.type) {
          case 'h1':
            return (
              <h1 key={idx} className="text-2xl font-bold text-dark-50 mt-2 mb-1 pb-2 border-b border-dark-800/60">
                {renderInline(block.content!)}
              </h1>
            )
          case 'h2':
            return (
              <h2 key={idx} className="text-xl font-semibold text-dark-50 mt-6 mb-1">
                {renderInline(block.content!)}
              </h2>
            )
          case 'h3':
            return (
              <h3 key={idx} className="text-base font-semibold text-accent-300 mt-4 mb-1">
                {renderInline(block.content!)}
              </h3>
            )
          case 'h4':
            return (
              <h4 key={idx} className="text-sm font-semibold text-dark-100 mt-3 mb-1 uppercase tracking-wide">
                {renderInline(block.content!)}
              </h4>
            )
          case 'p':
            return (
              <p key={idx} className="text-[15px] text-dark-200">
                {renderInline(block.content!)}
              </p>
            )
          case 'ul':
            return (
              <ul key={idx} className="space-y-1.5 pl-1">
                {block.items!.map((item, j) => (
                  <li key={j} className="flex gap-2 text-[15px] text-dark-200">
                    <span className="text-accent-400 select-none mt-0.5">•</span>
                    <span className="flex-1">{renderInline(item)}</span>
                  </li>
                ))}
              </ul>
            )
          case 'ol':
            return (
              <ol key={idx} className="space-y-1.5 pl-1 counter-reset-item">
                {block.items!.map((item, j) => (
                  <li key={j} className="flex gap-2 text-[15px] text-dark-200">
                    <span className="text-accent-400 select-none font-mono text-sm mt-0.5">{j + 1}.</span>
                    <span className="flex-1">{renderInline(item)}</span>
                  </li>
                ))}
              </ol>
            )
          case 'code':
            return (
              <pre
                key={idx}
                className="overflow-x-auto rounded-xl bg-dark-950/80 border border-dark-800/60 px-4 py-3 text-xs font-mono text-dark-100"
              >
                <code dangerouslySetInnerHTML={{ __html: escapeHtml(block.content ?? '') }} />
              </pre>
            )
          case 'blockquote':
            return (
              <blockquote
                key={idx}
                className="border-l-2 border-accent-500/60 pl-4 py-1 text-dark-300 italic text-[15px] bg-accent-500/5 rounded-r"
              >
                {renderInline(block.content!)}
              </blockquote>
            )
          case 'hr':
            return <hr key={idx} className="border-dark-800/60" />
          default:
            return <Fragment key={idx} />
        }
      })}
    </div>
  )
}
