import { CheckCircle2, Clock3, Download, ExternalLink, FileText, Highlighter, RotateCcw, X, XCircle } from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import { useCallback, useEffect, useId, useRef, useState, type MouseEvent, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useApp } from '../context/AppContext'
import { fetchDocumentContent, listDocumentVersions, makeDocumentVersionCurrent, uploadDocumentVersion, type DocumentVersion } from '../services/api'
import type { PolicyDocument } from '../types'
import PreviewContent, { PreviewLoading } from './document-preview/PreviewContent'
import { sourceFromDocument, sourceFromFile, type PreviewSource } from './document-preview/previewTypes'
import { Button } from './ui/Button'

interface DocumentPreviewModalProps {
  document?: PolicyDocument | null
  file?: File | null
  open?: boolean
  onClose?: () => void
}

export default function DocumentPreviewModal({ document: providedDocument, file, open, onClose }: DocumentPreviewModalProps = {}) {
  const { selectedDocument, setSelectedDocument, retrievedDocuments, refreshDocuments, showToast } = useApp()
  const document = file ? null : providedDocument ?? selectedDocument
  const source: PreviewSource | null = file ? sourceFromFile(file) : document ? sourceFromDocument(document) : null
  const isOpen = open ?? Boolean(source)
  const close = useCallback(() => onClose ? onClose() : setSelectedDocument(null), [onClose, setSelectedDocument])
  const titleId = useId()
  const dialogRef = useRef<HTMLDivElement>(null)
  const [blob, setBlob] = useState<Blob | null>(null)
  const [objectUrl, setObjectUrl] = useState('')
  const [loading, setLoading] = useState(false)
  const [previewError, setPreviewError] = useState('')
  const [retryKey, setRetryKey] = useState(0)
  const [versions, setVersions] = useState<DocumentVersion[]>([])
  const [loadingVersions, setLoadingVersions] = useState(false)
  const [versionError, setVersionError] = useState('')
  const [uploadingVersion, setUploadingVersion] = useState(false)

  useEffect(() => {
    if (!isOpen || !source) return
    const controller = new AbortController()
    let url = ''
    setBlob(null)
    setObjectUrl('')
    setPreviewError('')
    setLoading(true)
    const request = source.file ? Promise.resolve(source.file as Blob) : fetchDocumentContent(source.documentId!, controller.signal)
    void request.then(nextBlob => {
      if (controller.signal.aborted) return
      url = URL.createObjectURL(nextBlob)
      setBlob(nextBlob)
      setObjectUrl(url)
    }).catch(error => {
      if (!controller.signal.aborted) setPreviewError(error instanceof Error ? error.message : 'Unable to load this document preview.')
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => {
      controller.abort()
      if (url) URL.revokeObjectURL(url)
    }
  }, [isOpen, retryKey, source?.documentId, source?.file])

  useEffect(() => {
    if (!isOpen || !document?.uploaded) {
      setVersions([])
      return
    }
    let active = true
    setLoadingVersions(true)
    setVersionError('')
    void listDocumentVersions(document.id)
      .then(result => { if (active) setVersions(result.versions) })
      .catch(error => { if (active) setVersionError(error instanceof Error ? error.message : 'Unable to load version history.') })
      .finally(() => { if (active) setLoadingVersions(false) })
    return () => { active = false }
  }, [document?.id, document?.uploaded, isOpen])

  useEffect(() => {
    if (!isOpen) return
    const previousActive = documentGlobal.activeElement as HTMLElement | null
    const previousOverflow = documentGlobal.body.style.overflow
    documentGlobal.body.style.overflow = 'hidden'
    const dialog = dialogRef.current
    const focusable = () => Array.from(dialog?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])') ?? [])
    window.setTimeout(() => focusable()[0]?.focus(), 0)
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        close()
        return
      }
      if (event.key !== 'Tab') return
      const elements = focusable()
      if (!elements.length) return
      const first = elements[0]
      const last = elements[elements.length - 1]
      if (event.shiftKey && documentGlobal.activeElement === first) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && documentGlobal.activeElement === last) { event.preventDefault(); first.focus() }
    }
    documentGlobal.addEventListener('keydown', keydown)
    return () => {
      documentGlobal.removeEventListener('keydown', keydown)
      documentGlobal.body.style.overflow = previousOverflow
      previousActive?.focus()
    }
  }, [close, isOpen])

  if (!source) return null
  const reference = document ? retrievedDocuments.find(item => item.id === document.id || item.name === document.name) : null

  const makeCurrent = async (version: DocumentVersion) => {
    if (!document) return
    await makeDocumentVersionCurrent(document.id, version.id)
    setVersions(previous => previous.map(item => ({ ...item, is_current: item.id === version.id })))
    await refreshDocuments()
    setRetryKey(value => value + 1)
    showToast(`Version ${version.version_number} is now current`)
  }

  const addVersion = async (nextFile: File) => {
    if (!document) return
    setUploadingVersion(true)
    setVersionError('')
    try {
      await uploadDocumentVersion(document.id, nextFile)
      const result = await listDocumentVersions(document.id)
      setVersions(result.versions)
      await refreshDocuments()
      setRetryKey(value => value + 1)
      showToast('New document version processed')
    } catch (error) {
      setVersionError(error instanceof Error ? error.message : 'Version upload failed.')
    } finally {
      setUploadingVersion(false)
    }
  }

  const download = () => {
    if (!objectUrl) return
    const link = documentGlobal.createElement('a')
    link.href = objectUrl
    link.download = source.name
    documentGlobal.body.appendChild(link)
    link.click()
    link.remove()
  }
  const openNewTab = () => { if (objectUrl) window.open(objectUrl, '_blank', 'noopener,noreferrer') }
  const closeFromBackdrop = (event: MouseEvent<HTMLDivElement>) => { if (event.target === event.currentTarget) close() }

  return createPortal(
    <AnimatePresence>{isOpen && <motion.div className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-950/45 p-2 backdrop-blur-[3px] sm:p-5" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onMouseDown={closeFromBackdrop}>
      <motion.div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby={titleId} className="flex h-[90vh] w-[92vw] max-w-[1500px] flex-col overflow-hidden rounded-[20px] border border-white/70 bg-white shadow-[0_30px_100px_rgba(15,23,42,.35)] dark:border-slate-700 dark:bg-slate-900" initial={{ opacity: 0, y: 12, scale: .985 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 8, scale: .99 }} transition={{ duration: .2 }} onMouseDown={event => event.stopPropagation()}>
        <header className="flex min-h-[68px] shrink-0 items-center gap-3 border-b border-slate-200 px-4 sm:px-5">
          <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-blue-600 to-indigo-500 text-white shadow-[0_5px_14px_rgba(37,99,235,.18)]"><FileText size={19} /></span>
          <div className="min-w-0 flex-1"><h2 id={titleId} className="truncate text-sm font-bold text-slate-900 dark:text-slate-100">{source.name}</h2><p className="mt-0.5 text-[10px] font-semibold uppercase tracking-wide text-slate-400">{source.type || 'FILE'} <span aria-hidden="true">·</span> {source.size}</p></div>
          <HeaderAction label="Download" onClick={download} disabled={!objectUrl}><Download size={17} /></HeaderAction>
          <HeaderAction label="Open in new tab" onClick={openNewTab} disabled={!objectUrl}><ExternalLink size={17} /></HeaderAction>
          <HeaderAction label="Close preview" onClick={close}><X size={19} /></HeaderAction>
        </header>

        {reference && <div className="flex shrink-0 items-center gap-2 border-b border-yellow-200 bg-yellow-50 px-5 py-2 text-[11px] font-semibold text-yellow-800"><Highlighter size={14} />Cited source: {reference.section}</div>}
        <div className="flex min-h-0 flex-1">
          <main className="min-w-0 flex-1 bg-white dark:bg-slate-900">
            {loading && <PreviewLoading />}
            {previewError && <div className="grid h-full min-h-64 place-items-center px-6 text-center"><div><XCircle className="mx-auto text-red-400" size={38} /><p className="mt-3 text-sm font-bold text-slate-700">Preview failed to load</p><p className="mt-1 max-w-md text-xs leading-5 text-slate-500">{previewError}</p><Button className="mt-4" onClick={() => setRetryKey(value => value + 1)}>Try again</Button></div></div>}
            {!loading && !previewError && blob && objectUrl && <PreviewContent source={source} blob={blob} objectUrl={objectUrl} />}
          </main>
          {document?.uploaded && <aside className="hidden w-72 shrink-0 overflow-y-auto border-l border-slate-200 bg-slate-50/70 p-4 xl:block" aria-label="Document versions">
            <div className="flex items-center justify-between gap-2"><p className="text-xs font-bold text-slate-700">Version history</p><label className="cursor-pointer rounded-lg bg-blue-50 px-2.5 py-1.5 text-[10px] font-semibold text-blue-700 hover:bg-blue-100">{uploadingVersion ? 'Processing…' : 'New version'}<input type="file" className="hidden" disabled={uploadingVersion} onChange={event => { const nextFile = event.target.files?.[0]; if (nextFile) void addVersion(nextFile); event.currentTarget.value = '' }} /></label></div>
            {loadingVersions && <p className="mt-3 text-xs text-slate-400">Loading versions…</p>}
            {versionError && <p className="mt-3 text-xs text-red-600">{versionError}</p>}
            <div className="mt-3 space-y-2">{versions.map(version => <div key={version.id} className="rounded-xl border border-slate-200 bg-white p-3 text-xs"><div className="flex items-center gap-2">{version.status === 'completed' ? <CheckCircle2 size={15} className="text-emerald-600" /> : version.status === 'failed' ? <XCircle size={15} className="text-red-600" /> : <Clock3 size={15} className="text-blue-600" />}<p className="min-w-0 flex-1 font-semibold">Version {version.version_number}{version.is_current ? ' · Current' : ''}</p>{!version.is_current && version.status === 'completed' && <button type="button" onClick={() => void makeCurrent(version)} className="grid h-7 w-7 place-items-center rounded-lg text-blue-600 hover:bg-blue-50" aria-label={`Use version ${version.version_number}`}><RotateCcw size={13} /></button>}</div><p className="mt-1 truncate text-[10px] text-slate-500">{version.error?.message ?? version.status}</p></div>)}</div>
          </aside>}
        </div>
      </motion.div>
    </motion.div>}</AnimatePresence>,
    documentGlobal.body,
  )
}

const documentGlobal = document

function HeaderAction({ label, onClick, disabled, children }: { label: string; onClick: () => void; disabled?: boolean; children: ReactNode }) {
  return <button type="button" onClick={onClick} disabled={disabled} className="grid h-9 w-9 shrink-0 place-items-center rounded-xl text-slate-500 hover:bg-blue-50 hover:text-blue-600 disabled:cursor-not-allowed disabled:opacity-35 sm:flex sm:w-auto sm:gap-2 sm:px-3" aria-label={label}>{children}<span className="hidden text-[11px] font-semibold sm:inline">{label === 'Open in new tab' ? 'Open' : label}</span></button>
}
