import { useEffect, useState } from 'react'

interface SheetData { name: string; rows: string[][] }

export default function SpreadsheetPreview({ blob }: { blob: Blob }) {
  const [sheets, setSheets] = useState<SheetData[]>([])
  const [active, setActive] = useState(0)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const XLSX = await import('xlsx')
        const workbook = XLSX.read(await blob.arrayBuffer(), { type: 'array', cellDates: true })
        const parsed = workbook.SheetNames.map(name => ({
          name,
          rows: XLSX.utils.sheet_to_json<string[]>(workbook.Sheets[name], { header: 1, raw: false, defval: '' }),
        }))
        if (!cancelled) setSheets(parsed)
      } catch {
        if (!cancelled) setError('This spreadsheet could not be read.')
      }
    })()
    return () => { cancelled = true }
  }, [blob])

  if (error) return <p className="p-8 text-center text-xs font-semibold text-red-600">{error}</p>
  if (!sheets.length) return <p className="p-8 text-center text-xs text-slate-500">Reading workbook…</p>
  const sheet = sheets[active]
  const columns = Math.max(1, ...sheet.rows.map(row => row.length))
  return <div className="flex h-full min-h-0 flex-col">
    <div className="flex shrink-0 gap-1 overflow-x-auto border-b border-slate-200 bg-slate-50 px-3 pt-2" role="tablist" aria-label="Workbook sheets">
      {sheets.map((item, index) => <button key={item.name} type="button" role="tab" aria-selected={active === index} onClick={() => setActive(index)} className={`shrink-0 rounded-t-lg px-3 py-2 text-[11px] font-semibold ${active === index ? 'border border-b-white border-slate-200 bg-white text-blue-600' : 'text-slate-500 hover:bg-white/70'}`}>{item.name}</button>)}
    </div>
    <div className="min-h-0 flex-1 overflow-auto">
      <table className="preview-sheet-table">
        <thead><tr><th aria-label="Row number" className="preview-sheet-table__row-number" />{Array.from({ length: columns }, (_, index) => <th key={index}>{columnName(index)}</th>)}</tr></thead>
        <tbody>{sheet.rows.map((row, rowIndex) => <tr key={rowIndex}><th className="preview-sheet-table__row-number">{rowIndex + 1}</th>{Array.from({ length: columns }, (_, columnIndex) => <td key={columnIndex}>{row[columnIndex] ?? ''}</td>)}</tr>)}</tbody>
      </table>
      {!sheet.rows.length && <p className="p-8 text-center text-xs text-slate-400">This sheet is empty.</p>}
    </div>
  </div>
}

function columnName(index: number) {
  let value = index + 1
  let output = ''
  while (value > 0) {
    value -= 1
    output = String.fromCharCode(65 + value % 26) + output
    value = Math.floor(value / 26)
  }
  return output
}
