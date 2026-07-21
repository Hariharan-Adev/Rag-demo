import { Fragment, type ReactNode } from 'react'

function inlineMarkdown(text: string): ReactNode[] {
  return text.split(/(`[^`]+`|\[[^\]]+\]\([^)]+\))/g).filter(Boolean).map((part, index) => {
    if (part.startsWith('`') && part.endsWith('`')) return <code key={index}>{part.slice(1, -1)}</code>
    const link = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/)
    if (link) {
      const safeHref = /^https?:\/\//i.test(link[2]) ? link[2] : '#'
      return <a key={index} href={safeHref} target="_blank" rel="noreferrer">{link[1]}</a>
    }
    return <Fragment key={index}>{part}</Fragment>
  })
}

function renderTextBlocks(text: string, keyPrefix: string) {
  const lines = text.split('\n')
  const blocks: ReactNode[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index].trim()
    if (!line) { index += 1; continue }

    const heading = line.match(/^(#{1,6})\s+(.+)$/)
    if (heading) {
      const level = Math.min(heading[1].length, 4)
      const Tag = `h${level}` as keyof JSX.IntrinsicElements
      blocks.push(<Tag key={`${keyPrefix}-${index}`}>{inlineMarkdown(heading[2])}</Tag>)
      index += 1
      continue
    }

    if (/^[-*]\s+/.test(line)) {
      const items: string[] = []
      while (index < lines.length && /^[-*]\s+/.test(lines[index].trim())) items.push(lines[index++].trim().replace(/^[-*]\s+/, ''))
      blocks.push(<ul key={`${keyPrefix}-${index}`}>{items.map((item, itemIndex) => <li key={itemIndex}>{inlineMarkdown(item)}</li>)}</ul>)
      continue
    }

    if (/^\d+\.\s+/.test(line)) {
      const items: string[] = []
      while (index < lines.length && /^\d+\.\s+/.test(lines[index].trim())) items.push(lines[index++].trim().replace(/^\d+\.\s+/, ''))
      blocks.push(<ol key={`${keyPrefix}-${index}`}>{items.map((item, itemIndex) => <li key={itemIndex}>{inlineMarkdown(item)}</li>)}</ol>)
      continue
    }

    if (line.startsWith('> ')) {
      const quotes: string[] = []
      while (index < lines.length && lines[index].trim().startsWith('> ')) quotes.push(lines[index++].trim().slice(2))
      blocks.push(<blockquote key={`${keyPrefix}-${index}`}>{inlineMarkdown(quotes.join(' '))}</blockquote>)
      continue
    }

    const isTable = line.includes('|') && index + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[index + 1])
    if (isTable) {
      const parseRow = (row: string) => row.replace(/^\||\|$/g, '').split('|').map(cell => cell.trim())
      const headers = parseRow(lines[index])
      index += 2
      const rows: string[][] = []
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) rows.push(parseRow(lines[index++]))
      blocks.push(<div className="table-scroll" key={`${keyPrefix}-${index}`}><table><thead><tr>{headers.map((header, cellIndex) => <th key={cellIndex}>{inlineMarkdown(header)}</th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{inlineMarkdown(cell)}</td>)}</tr>)}</tbody></table></div>)
      continue
    }

    const paragraph = [line]
    index += 1
    while (index < lines.length && lines[index].trim() && !/^(#{1,6})\s+|^[-*]\s+|^\d+\.\s+|^>\s+/.test(lines[index].trim())) paragraph.push(lines[index++].trim())
    blocks.push(<p key={`${keyPrefix}-${index}`}>{inlineMarkdown(paragraph.join(' '))}</p>)
  }

  return blocks
}

export default function MarkdownContent({ content }: { content: string }) {
  const segments = content.split(/(```[\s\S]*?```)/g).filter(Boolean)
  return <div className="markdown-content">{segments.map((segment, index) => {
    if (segment.startsWith('```')) {
      const inner = segment.slice(3, -3).replace(/^\w+\n/, '')
      return <pre key={index}><code>{inner.trim()}</code></pre>
    }
    return <Fragment key={index}>{renderTextBlocks(segment, String(index))}</Fragment>
  })}</div>
}
