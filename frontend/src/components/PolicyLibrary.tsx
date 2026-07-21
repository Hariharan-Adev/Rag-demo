import { FileText, Search, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useApp } from '../context/AppContext'
import { cn } from '../utils/cn'

export default function PolicyLibrary({ onViewAll }: { onViewAll: () => void }) {
  const { documents, selectedDocument, setSelectedDocument } = useApp()
  const [search, setSearch] = useState('')
  const filtered = useMemo(() => documents.filter(document => document.name.toLowerCase().includes(search.toLowerCase())), [documents, search])

  return (
    <section aria-labelledby="document-library-title">
      <div className="flex items-center justify-between px-2">
        <h2 id="document-library-title" className="text-[10px] font-semibold uppercase tracking-[.12em] text-slate-400">Document Library</h2>
        <span className="text-[10px] text-slate-400">{documents.length}</span>
      </div>
      <div className="relative mt-2">
        <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
        <input id="policy-search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search documents" className="h-9 w-full rounded-xl border border-[#e6ecf5] bg-white pl-9 pr-8 text-[11px] shadow-[0_2px_8px_rgba(15,23,42,.03)] outline-none placeholder:text-slate-400 focus:border-blue-500 focus:ring-4 focus:ring-blue-100/60" aria-label="Search documents" />
        {search && <button type="button" onClick={() => setSearch('')} className="absolute right-2 top-1/2 grid h-6 w-6 -translate-y-1/2 place-items-center rounded-md text-slate-400 hover:bg-blue-50 hover:text-blue-600" aria-label="Clear document search"><X size={13} /></button>}
      </div>
      <div className="mt-2 space-y-0.5">
        {filtered.slice(0, 6).map(document => (
          <button key={document.id} type="button" onClick={() => setSelectedDocument(document)} className={cn('flex h-9 w-full items-center gap-2 rounded-[10px] px-2 text-left text-[11px] text-slate-600 hover:bg-[#f8fbff] hover:text-blue-600', selectedDocument?.id === document.id && 'bg-[#eef4ff] text-blue-600')}>
            <FileText size={14} className="shrink-0 text-blue-500" />
            <span className="truncate">{document.name}</span>
          </button>
        ))}
        {filtered.length === 0 && <p className="px-2 py-3 text-center text-[10px] text-slate-400">{documents.length ? 'No matching documents' : 'No documents yet'}</p>}
      </div>
      <button type="button" onClick={onViewAll} className="mt-1 h-8 w-full rounded-[10px] px-2 text-left text-[11px] font-medium text-slate-500 hover:bg-[#f3f7ff] hover:text-blue-600">View all documents</button>
    </section>
  )
}
