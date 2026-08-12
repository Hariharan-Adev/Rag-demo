import { ChevronDown, FileText, FolderOpen, Menu, Plus, Search, Trash2, Upload } from 'lucide-react'
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
  const { documents, collections, selectedCollectionId, setSelectedCollectionId, setSelectedDocument, removeDocument, setSidebarOpen } = useApp()
  const [search, setSearch] = useState('')
  const [tab, setTab] = useState<'all' | 'documents'>('all')
  const [documentToDelete, setDocumentToDelete] = useState<PolicyDocument | null>(null)
  const [selectedDocumentIds, setSelectedDocumentIds] = useState<string[]>([])
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false)
  const [isDeleting, setIsDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const deleteTriggerRef = useRef<HTMLButtonElement | null>(null)
  const headerSelectAllRef = useRef<HTMLInputElement | null>(null)
  const toolbarSelectAllRef = useRef<HTMLInputElement | null>(null)
  const filtered = useMemo(() => [...documents]
    .sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime())
    .filter(document => selectedCollectionId === null || document.collectionId === selectedCollectionId)
    .filter(document => document.name.toLowerCase().includes(search.trim().toLowerCase())), [documents, search, selectedCollectionId])
  const visibleDocumentIds = useMemo(() => filtered.map(document => document.id), [filtered])
  const visibleDocumentIdSet = useMemo(() => new Set(visibleDocumentIds), [visibleDocumentIds])
  const selectedVisibleCount = selectedDocumentIds.filter(id => visibleDocumentIdSet.has(id)).length
  const allVisibleSelected = filtered.length > 0 && selectedVisibleCount === filtered.length
  const someVisibleSelected = selectedVisibleCount > 0 && !allVisibleSelected

  useEffect(() => {
    if (headerSelectAllRef.current) headerSelectAllRef.current.indeterminate = someVisibleSelected
    if (toolbarSelectAllRef.current) toolbarSelectAllRef.current.indeterminate = someVisibleSelected
  }, [someVisibleSelected])

  useEffect(() => {
    setSelectedDocumentIds(previous => previous.filter(id => visibleDocumentIdSet.has(id)))
  }, [visibleDocumentIdSet])

  const requestDelete = (document: PolicyDocument, event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation()
    deleteTriggerRef.current = event.currentTarget
    setDeleteError('')
    setBulkDeleteOpen(false)
    setDocumentToDelete(document)
  }

  const toggleDocumentSelection = (documentId: string, checked: boolean) => {
    setSelectedDocumentIds(previous => {
      if (checked) return previous.includes(documentId) ? previous : [...previous, documentId]
      return previous.filter(id => id !== documentId)
    })
  }

  const toggleAllVisibleDocuments = (checked: boolean) => {
    setSelectedDocumentIds(checked ? visibleDocumentIds : [])
  }

  const requestBulkDelete = (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation()
    if (selectedVisibleCount === 0) return
    deleteTriggerRef.current = event.currentTarget
    setDeleteError('')
    setDocumentToDelete(null)
    setBulkDeleteOpen(true)
  }

  const cancelDelete = () => {
    if (isDeleting) return
    setDocumentToDelete(null)
    setBulkDeleteOpen(false)
    setDeleteError('')
    window.setTimeout(() => deleteTriggerRef.current?.focus(), 0)
  }

  const confirmDelete = async () => {
    if (isDeleting) return
    setIsDeleting(true)
    setDeleteError('')
    try {
      if (bulkDeleteOpen) {
        const targets = selectedDocumentIds.filter(id => visibleDocumentIdSet.has(id))
        const failedIds: string[] = []
        for (const id of targets) {
          try {
            await removeDocument(id)
          } catch {
            failedIds.push(id)
          }
        }
        setSelectedDocumentIds(failedIds)
        if (failedIds.length > 0) {
          const deletedCount = targets.length - failedIds.length
          setDeleteError(deletedCount > 0 ? `${deletedCount} deleted. ${failedIds.length} could not be deleted.` : 'Unable to delete the selected documents. Please try again.')
          return
        }
        setBulkDeleteOpen(false)
      } else if (documentToDelete) {
        await removeDocument(documentToDelete.id)
        setDocumentToDelete(null)
      }
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

      {collections.length > 0 && <div className="mb-4 flex gap-2 overflow-x-auto pb-1">
        <button type="button" onClick={() => setSelectedCollectionId(null)} className={`flex shrink-0 items-center gap-2 rounded-xl border px-3 py-2 text-xs font-semibold ${selectedCollectionId === null ? 'border-blue-200 bg-blue-50 text-blue-700' : 'border-slate-200 bg-white text-slate-600'}`}><FolderOpen size={15} />All documents</button>
        {collections.map(collection => <button key={collection.id} type="button" onClick={() => setSelectedCollectionId(collection.id)} className={`flex shrink-0 items-center gap-2 rounded-xl border px-3 py-2 text-xs font-semibold ${selectedCollectionId === collection.id ? 'border-blue-200 bg-blue-50 text-blue-700' : 'border-slate-200 bg-white text-slate-600'}`}><FolderOpen size={15} />{collection.name}<span className="text-[10px] text-slate-400">{collection.document_count ?? 0}</span></button>)}
      </div>}

      <div className="mb-4 flex gap-2 sm:hidden">
        <div className="relative min-w-0 flex-1"><Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" /><input id="library-search-mobile" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search documents" className="h-10 w-full rounded-xl border border-[#e6ecf5] bg-white pl-9 pr-3 text-[12px] outline-none focus:border-blue-500 focus:ring-4 focus:ring-blue-100/60" /></div>
        <NewMenu onUpload={onUpload} />
      </div>

      {selectedVisibleCount > 0 && <div className="mb-2 flex min-h-11 items-center gap-3 rounded-xl border border-blue-100 bg-blue-50/70 px-3 text-[12px] font-semibold text-slate-700">
        <input ref={toolbarSelectAllRef} type="checkbox" checked={allVisibleSelected} onChange={event => toggleAllVisibleDocuments(event.currentTarget.checked)} className="h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500" aria-label="Select all documents" />
        <span className="min-w-0 flex-1">{selectedVisibleCount} selected</span>
        <button type="button" disabled={isDeleting} onClick={requestBulkDelete} className="grid h-8 w-8 place-items-center rounded-lg text-slate-500 hover:bg-red-50 hover:text-red-600 disabled:cursor-not-allowed disabled:opacity-50" aria-label="Delete selected documents"><Trash2 size={15} /></button>
      </div>}

      <div className="hidden grid-cols-[28px_minmax(0,1fr)_150px_110px_44px] gap-3 border-b border-[#e6ecf5] px-3 pb-2 text-[10px] font-semibold uppercase tracking-[.08em] text-slate-400 sm:grid">
        <input ref={headerSelectAllRef} type="checkbox" checked={allVisibleSelected} disabled={!filtered.length} onChange={event => toggleAllVisibleDocuments(event.currentTarget.checked)} className="h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500 disabled:opacity-40" aria-label="Select all documents" />
        <span>Name</span><span>Modified</span><span>Size</span><span />
      </div>
      <div className="mt-2 space-y-2">
        {filtered.map(document => {
          const isSelected = selectedDocumentIds.includes(document.id)
          return <article key={document.id} className={`relative grid grid-cols-[28px_minmax(0,1fr)_44px] items-center gap-3 rounded-2xl border p-3 shadow-[0_5px_18px_rgba(37,99,235,.04)] transition hover:-translate-y-0.5 hover:border-blue-100 hover:shadow-[0_8px_24px_rgba(37,99,235,.07)] sm:grid-cols-[28px_minmax(0,1fr)_150px_110px_44px] ${isSelected ? 'border-blue-100 bg-blue-50/60' : 'border-[#eef2f7] bg-white'}`}>
            <input type="checkbox" checked={isSelected} onChange={event => toggleDocumentSelection(document.id, event.currentTarget.checked)} onClick={event => event.stopPropagation()} className="h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500" aria-label={`Select document ${document.name}`} />
            <button type="button" onClick={() => setSelectedDocument(document)} className="flex min-w-0 items-center gap-3 text-left">
              <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-50 text-blue-600"><FileText size={18} /></span>
              <span className="min-w-0"><span className="block truncate text-[12px] font-semibold text-slate-800">{document.name}</span><span className="mt-0.5 block text-[10px] text-slate-400 sm:hidden">{relativeDate(document.updatedAt)} - {document.size}</span></span>
            </button>
            <span className="hidden text-[11px] text-slate-500 sm:block">{relativeDate(document.updatedAt)}</span>
            <span className="hidden text-[11px] text-slate-500 sm:block">{document.size}</span>
            <button type="button" disabled={isDeleting && documentToDelete?.id === document.id} onClick={event => requestDelete(document, event)} className="grid h-8 w-8 place-items-center rounded-lg text-slate-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-50" aria-label={`Delete ${document.name}`}><Trash2 size={14} /></button>
          </article>
        })}
        {!filtered.length && <div className="rounded-2xl border border-dashed border-[#d9e3f1] bg-white/60 px-4 py-16 text-center"><FileText className="mx-auto text-slate-300" size={28} /><p className="mt-3 text-[13px] font-semibold text-slate-600">No documents found.</p></div>}
      </div>
    </div>
    <DocumentDeleteModal open={documentToDelete !== null || bulkDeleteOpen} documentName={documentToDelete?.name ?? ''} documentCount={bulkDeleteOpen ? selectedVisibleCount : undefined} isDeleting={isDeleting} error={deleteError} onCancel={cancelDelete} onConfirm={() => void confirmDelete()} />
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
