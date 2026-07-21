import { ChevronDown, FileText, Menu, Plus, Search, Trash2, Upload } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { useApp } from '../context/AppContext'
import type { PolicyDocument } from '../types'
import DocumentDeleteModal from './DocumentDeleteModal'

function relativeDate(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const target = new Date(date)
  target.setHours(0, 0, 0, 0)
  const days = Math.floor((today.getTime() - target.getTime()) / 86_400_000)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 7) return `${days} days ago`
  return date.toLocaleDateString()
}

export default function LibraryPage({ onUpload }: { onUpload: () => void }) {
  const { documents, setSelectedDocument, removeDocument, setSidebarOpen } = useApp()
  const [search, setSearch] = useState('')
  const [tab, setTab] = useState<'all' | 'documents'>('all')
  const [documentToDelete, setDocumentToDelete] = useState<PolicyDocument | null>(null)
  const [isDeleting, setIsDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const deleteTriggerRef = useRef<HTMLButtonElement | null>(null)
  const filtered = useMemo(() => [...documents]
    .sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime())
    .filter(document => document.name.toLowerCase().includes(search.trim().toLowerCase())), [documents, search])

  const requestDelete = (document: PolicyDocument, event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation()
    deleteTriggerRef.current = event.currentTarget
    setDeleteError('')
    setDocumentToDelete(document)
  }

  const cancelDelete = () => {
    if (isDeleting) return
    setDocumentToDelete(null)
    setDeleteError('')
    window.setTimeout(() => deleteTriggerRef.current?.focus(), 0)
  }

  const confirmDelete = async () => {
    if (!documentToDelete || isDeleting) return
    setIsDeleting(true)
    setDeleteError('')
    try {
      await removeDocument(documentToDelete.id)
      setDocumentToDelete(null)
    } catch (error) {
      setDeleteError(error instanceof Error && error.message ? error.message : 'Unable to delete the document. Please try again.')
    } finally {
      setIsDeleting(false)
    }
  }

  return <section className="min-w-0 flex-1 overflow-y-auto bg-[#f8fafc] px-4 py-5 sm:px-7 sm:py-7">
    <div className="mx-auto max-w-[1000px]">
      <div className="mb-7 flex items-center gap-3">
        <button type="button" onClick={() => setSidebarOpen(true)} className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-white text-slate-500 shadow-sm hover:bg-blue-50 hover:text-blue-600 lg:hidden" aria-label="Open sidebar"><Menu size={20} /></button>
        <h1 className="text-2xl font-bold tracking-[-.035em] text-slate-900 sm:text-[28px]">Documents</h1>
        <div className="ml-auto hidden items-center gap-2 sm:flex">
          <div className="relative">
            <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
            <input id="library-search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search documents" className="h-10 w-64 rounded-xl border border-[#e6ecf5] bg-white pl-9 pr-3 text-[12px] outline-none focus:border-blue-500 focus:ring-4 focus:ring-blue-100/60" />
          </div>
          <NewMenu onUpload={onUpload} />
        </div>
      </div>

      <div className="mb-5 flex items-center gap-1 border-b border-[#e6ecf5]">
        {(['all', 'documents'] as const).map(value => <button key={value} type="button" onClick={() => setTab(value)} className={`relative px-4 py-2.5 text-[12px] font-semibold capitalize ${tab === value ? 'text-blue-600 after:absolute after:inset-x-2 after:bottom-0 after:h-0.5 after:rounded-full after:bg-blue-600' : 'text-slate-500 hover:text-slate-900'}`}>{value}</button>)}
      </div>

      <div className="mb-4 flex gap-2 sm:hidden">
        <div className="relative min-w-0 flex-1"><Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" /><input id="library-search-mobile" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search documents" className="h-10 w-full rounded-xl border border-[#e6ecf5] bg-white pl-9 pr-3 text-[12px] outline-none focus:border-blue-500 focus:ring-4 focus:ring-blue-100/60" /></div>
        <NewMenu onUpload={onUpload} />
      </div>

      <div className="hidden grid-cols-[minmax(0,1fr)_150px_110px_44px] gap-3 border-b border-[#e6ecf5] px-3 pb-2 text-[10px] font-semibold uppercase tracking-[.08em] text-slate-400 sm:grid"><span>Name</span><span>Modified</span><span>Size</span><span /></div>
      <div className="mt-2 space-y-2">
        {filtered.map(document => <article key={document.id} className="relative grid gap-2 rounded-2xl border border-[#eef2f7] bg-white p-3 shadow-[0_5px_18px_rgba(37,99,235,.04)] transition hover:-translate-y-0.5 hover:border-blue-100 hover:shadow-[0_8px_24px_rgba(37,99,235,.07)] sm:grid-cols-[minmax(0,1fr)_150px_110px_44px] sm:items-center sm:gap-3">
          <button type="button" onClick={() => setSelectedDocument(document)} className="flex min-w-0 items-center gap-3 text-left">
            <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-50 text-blue-600"><FileText size={18} /></span>
            <span className="min-w-0"><span className="block truncate text-[12px] font-semibold text-slate-800">{document.name}</span><span className="mt-0.5 block text-[10px] text-slate-400 sm:hidden">{relativeDate(document.updatedAt)} · {document.size}</span></span>
          </button>
          <span className="hidden text-[11px] text-slate-500 sm:block">{relativeDate(document.updatedAt)}</span>
          <span className="hidden text-[11px] text-slate-500 sm:block">{document.size}</span>
          <button type="button" disabled={isDeleting && documentToDelete?.id === document.id} onClick={event => requestDelete(document, event)} className="absolute right-6 mt-1 grid h-8 w-8 place-items-center rounded-lg text-slate-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-50 sm:static sm:mt-0" aria-label={`Delete ${document.name}`}><Trash2 size={14} /></button>
        </article>)}
        {!filtered.length && <div className="rounded-2xl border border-dashed border-[#d9e3f1] bg-white/60 px-4 py-16 text-center"><FileText className="mx-auto text-slate-300" size={28} /><p className="mt-3 text-[13px] font-semibold text-slate-600">No documents found.</p></div>}
      </div>
    </div>
    <DocumentDeleteModal open={documentToDelete !== null} documentName={documentToDelete?.name ?? ''} isDeleting={isDeleting} error={deleteError} onCancel={cancelDelete} onConfirm={() => void confirmDelete()} />
  </section>
}

function NewMenu({ onUpload }: { onUpload: () => void }) {
  const [open, setOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const outside = (event: PointerEvent) => { if (!menuRef.current?.contains(event.target as Node)) setOpen(false) }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape) }
  }, [open])

  return <div ref={menuRef} className="relative shrink-0">
    <button type="button" onClick={() => setOpen(!open)} aria-haspopup="menu" aria-expanded={open} className="flex h-10 items-center gap-2 rounded-xl bg-gradient-to-br from-blue-600 to-indigo-500 px-4 text-[12px] font-semibold text-white shadow-[0_5px_16px_rgba(37,99,235,.22)] hover:-translate-y-0.5"><Plus size={16} />New<ChevronDown size={13} /></button>
    {open && <div role="menu" className="absolute right-0 top-12 z-40 w-48 rounded-[14px] border border-[#e6ecf5] bg-white p-1.5 shadow-[0_10px_30px_rgba(15,23,42,.12)]"><button type="button" role="menuitem" onClick={() => { setOpen(false); onUpload() }} className="flex h-[42px] w-full items-center gap-2.5 rounded-[9px] px-3 text-left text-[12px] font-medium text-slate-600 hover:bg-[#f3f7ff] hover:text-blue-600"><Upload size={16} />Upload</button></div>}
  </div>
}
