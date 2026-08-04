// Split a simple backend-generated Markdown table row into trimmed cells.
function splitMarkdownRow(line: string) {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map(cell => cell.trim())
}

// Rebuild a Markdown table after removing backend-only columns.
function buildMarkdownTable(headers: string[], rows: string[][]) {
  const headerLine = `| ${headers.join(' | ')} |`
  const delimiter = `|${headers.map(() => '---').join('|')}|`
  const rowLines = rows.map(row => `| ${row.join(' | ')} |`)
  return [headerLine, delimiter, ...rowLines].join('\n')
}

// Pick the actual answer cell from a single record while skipping prompts/citations.
function usefulSingleRecordCell(headers: string[], row: string[]) {
  const ignoredHeaders = new Set(['source', 'pick from list'])
  const ignoredValues = new Set(['', 'none', 'null', 'expected responsibilities'])
  const candidates = headers
    .map((header, index) => ({ header, value: row[index] ?? '' }))
    .filter(candidate => !ignoredHeaders.has(candidate.header.toLowerCase()))
    .filter(candidate => !ignoredValues.has(candidate.value.toLowerCase()))
    .filter(candidate => !candidate.value.toLowerCase().startsWith('what are your current'))

  if (!candidates.length) return null
  return candidates.sort((left, right) => right.value.length - left.value.length)[0]
}

// Normalize structured record answers without touching ordinary Markdown tables.
export function formatStructuredAnswer(content: string) {
  const lines = content.trim().split(/\r?\n/)
  if (!/^Matching records \(\d+\):/i.test(lines[0] ?? '')) return content

  const tableStart = lines.findIndex(line => line.trim().startsWith('|'))
  if (tableStart < 0 || tableStart + 2 >= lines.length) return content

  const headers = splitMarkdownRow(lines[tableStart])
  const rows = lines.slice(tableStart + 2).filter(line => line.trim().startsWith('|')).map(splitMarkdownRow)
  const sourceIndex = headers.findIndex(header => header.toLowerCase() === 'source')

  if (rows.length === 1) {
    const cell = usefulSingleRecordCell(headers, rows[0])
    return cell ? `${cell.header}: ${cell.value}` : content
  }

  if (sourceIndex < 0) return content
  const visibleHeaders = headers.filter((_, index) => index !== sourceIndex)
  const visibleRows = rows.map(row => row.filter((_, index) => index !== sourceIndex))
  return `${lines[0]}\n\n${buildMarkdownTable(visibleHeaders, visibleRows)}`
}
