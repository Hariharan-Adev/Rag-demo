import { FileText, RotateCcw, Search, Trash2 } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { useApp } from '../context/AppContext'
import type { PolicyDocument } from '../types'
import DocumentDeleteModal from './DocumentDeleteModal'
import { Modal } from './ui/Modal'
import { listDeletedDocuments, restoreDocument } from '../services/api'

export default function LibraryModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { documents, setSelectedDocument, removeDocument, refreshDocuments, showToast } = useApp()
  const [deletedDocuments, setDeletedDocuments] = useState<Array<{ id: number; display_filename: string; deleted_at: string }>>([])
  const [search, setSearch] = useState('')
  const [documentToDelete, setDocumentToDelete] = useState<PolicyDocument | null>(null)
  const [isDeleting, setIsDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const deleteTriggerRef = useRef<HTMLButtonElement | null>(null)
  const filtered = useMemo(() => documents.filter(doc => doc.name.toLowerCase().includes(search.toLowerCase())), [documents, search])

  useEffect(() => {
    if (open) void listDeletedDocuments().then(result => setDeletedDocuments(result.documents)).catch(() => setDeletedDocuments([]))
  }, [open, documents])

  const restore = async (id: number) => {
    await restoreDocument(String(id))
    setDeletedDocuments(previous => previous.filter(document => document.id !== id))
    await refreshDocuments()
    showToast('Document restored')
  }

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

  return (
    <Modal open={open} onClose={onClose} title="Document Library">
      <div className="relative mb-3">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
        <input value={search} onChange={event => setSearch(event.target.value)} placeholder="Search all documents" className="h-10 w-full rounded-xl border border-[#e6ecf5] bg-white pl-9 pr-3 text-xs outline-none focus:border-blue-500 focus:ring-4 focus:ring-blue-100/60" />
      </div>
      <div className="max-h-[55vh] space-y-2 overflow-y-auto">
        {filtered.map(doc => (
          <div key={doc.id} className="flex items-center rounded-xl border border-[#eef2f7] bg-white shadow-[0_4px_16px_rgba(37,99,235,.035)] transition hover:-translate-y-0.5 hover:bg-[#f8fbff]">
            <button onClick={() => setSelectedDocument(doc)} className="flex min-w-0 flex-1 items-center gap-3 p-3 text-left">
              <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-blue-50 text-blue-600"><FileText size={17} /></span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-semibold">{doc.name}</p>
                <p className="text-[10px] text-slate-500">{doc.type} - {doc.size}</p>
              </div>
              <span className="rounded-full bg-blue-50 px-2 py-1 text-[9px] font-semibold text-blue-600">OWNED</span>
            </button>
            <button
              disabled={isDeleting && documentToDelete?.id === doc.id}
              onClick={event => requestDelete(doc, event)}
              className="mr-3 grid h-8 w-8 shrink-0 place-items-center rounded-lg text-slate-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-50"
              aria-label={`Remove ${doc.name}`}
            >
              <Trash2 size={14} />
            </button>
          </div>
        ))}
        {filtered.length === 0 && <p className="rounded-xl border border-dashed border-slate-200 p-8 text-center text-xs text-slate-400">No search results</p>}
      </div>
      {deletedDocuments.length > 0 && <div className="mt-4 border-t border-slate-100 pt-3"><p className="mb-2 text-xs font-semibold text-slate-600">Recently deleted</p>{deletedDocuments.map(document => <div key={document.id} className="flex items-center gap-2 rounded-lg bg-slate-50 p-2 text-xs"><span className="min-w-0 flex-1 truncate">{document.display_filename}</span><button onClick={() => void restore(document.id)} className="flex items-center gap-1 rounded-md px-2 py-1 text-blue-600 hover:bg-blue-50"><RotateCcw size={13} />Restore</button></div>)}</div>}
      <DocumentDeleteModal open={documentToDelete !== null} documentName={documentToDelete?.name ?? ''} isDeleting={isDeleting} error={deleteError} onCancel={cancelDelete} onConfirm={() => void confirmDelete()} />
    </Modal>
  )
}
