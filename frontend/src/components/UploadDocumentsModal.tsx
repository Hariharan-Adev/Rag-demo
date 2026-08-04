import { AlertCircle, CheckCircle2, FileText, FolderOpen, RotateCcw, UploadCloud, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type ChangeEvent, type InputHTMLAttributes } from 'react'
import { useApp } from '../context/AppContext'
import {
  ApiError,
  cancelUploadBatch,
  createCollection,
  createUploadBatch,
  getUploadConfig,
  skipUploadBatchFiles,
  uploadDocument,
  uploadZipArchive,
  type UploadConfig,
  type UploadResponse,
  type ZipUploadResponse,
} from '../services/api'
import { Button } from './ui/Button'
import { Modal } from './ui/Modal'
import DocumentPreviewModal from './DocumentPreviewModal'

type FileStatus = 'pending' | 'uploading' | 'processing' | 'completed' | 'skipped' | 'failed' | 'cancelled'
type Validation = 'Ready' | 'Unsupported' | 'Too large' | 'Empty' | 'Duplicate candidate'
type FolderInputAttributes = InputHTMLAttributes<HTMLInputElement> & { webkitdirectory: string; directory: string }

interface UploadItem {
  id: string
  idempotencyKey: string
  file: File
  relativePath: string
  validation: Validation
  status: FileStatus
  result?: UploadResponse
  archiveResult?: ZipUploadResponse
  error?: string
  retryable?: boolean
}

const defaultConfig: UploadConfig = {
  supported_extensions: ['.txt', '.md', '.json', '.xml', '.html', '.htm', '.pdf', '.docx', '.xlsx', '.xls', '.csv', '.pptx', '.ppt', '.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff', '.webp'],
  archive_extensions: ['.zip'],
  max_file_size_mb: 25,
  max_zip_upload_mb: 50,
  max_folder_files: 25,
  max_folder_total_size_mb: 200,
  max_concurrent_uploads: 3,
}
const folderAttributes: FolderInputAttributes = { webkitdirectory: '', directory: '' }

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

function extension(file: File) {
  const suffix = file.name.includes('.') ? `.${file.name.split('.').pop()?.toLowerCase()}` : ''
  return suffix
}

export default function UploadDocumentsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { collections, documents, refreshDocuments, showToast } = useApp()
  const [mode, setMode] = useState<'files' | 'folder'>('files')
  const [config, setConfig] = useState(defaultConfig)
  const [items, setItems] = useState<UploadItem[]>([])
  const [folderName, setFolderName] = useState('')
  const [collectionChoice, setCollectionChoice] = useState<'new' | string>('new')
  const [newCollectionName, setNewCollectionName] = useState('')
  const [uploading, setUploading] = useState(false)
  const [batchId, setBatchId] = useState<number | null>(null)
  const [previewFile, setPreviewFile] = useState<File | null>(null)
  const cancelledRef = useRef(false)
  const controllersRef = useRef(new Map<string, AbortController>())
  const fileInputRef = useRef<HTMLInputElement>(null)
  const folderInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!open) return
    void getUploadConfig().then(setConfig).catch(() => setConfig(defaultConfig))
  }, [open])

  const summary = useMemo(() => ({
    completed: items.filter(item => item.status === 'completed' && !item.result?.duplicate_type).length,
    duplicates: items.filter(item => item.result?.duplicate_type || item.error?.toLowerCase().includes('already exists')).length,
    skipped: items.filter(item => item.status === 'skipped' || item.status === 'cancelled').length,
    failed: items.filter(item => item.status === 'failed').length,
    processed: items.filter(item => ['completed', 'skipped', 'failed', 'cancelled'].includes(item.status)).length,
  }), [items])

  const selectFiles = (incoming: File[], isFolder: boolean) => {
    const maxBytes = config.max_file_size_mb * 1024 * 1024
    const maxZipBytes = config.max_zip_upload_mb * 1024 * 1024
    const knownNames = new Set(documents.map(document => document.name.toLowerCase()))
    const selected = incoming.map(file => {
      const relativePath = isFolder && file.webkitRelativePath ? file.webkitRelativePath : file.name
      const suffix = extension(file)
      const isArchive = !isFolder && config.archive_extensions.includes(suffix)
      let validation: Validation = 'Ready'
      if (!config.supported_extensions.includes(suffix) && !isArchive) validation = 'Unsupported'
      else if (file.size === 0) validation = 'Empty'
      else if (file.size > (isArchive ? maxZipBytes : maxBytes)) validation = 'Too large'
      else if (knownNames.has(file.name.toLowerCase())) validation = 'Duplicate candidate'
      return {
        id: crypto.randomUUID(),
        idempotencyKey: crypto.randomUUID(),
        file, relativePath, validation,
        status: validation === 'Unsupported' || validation === 'Too large' || validation === 'Empty' ? 'skipped' as const : 'pending' as const,
      }
    })
    const root = selected[0]?.relativePath.split('/')[0] ?? ''
    if (isFolder) {
      setFolderName(root)
      setNewCollectionName(root)
    }
    setItems(selected)
  }

  const onFolderSelection = (event: ChangeEvent<HTMLInputElement>) => {
    selectFiles(Array.from(event.target.files ?? []), true)
    event.target.value = ''
  }

  const runUploads = async (targets: UploadItem[], includeSkipped = true) => {
    if (!targets.length || uploading) return
    const batchItems = includeSkipped ? items : targets
    const totalBytes = batchItems.reduce((total, item) => total + item.file.size, 0)
    if (mode === 'folder' && items.length > config.max_folder_files) {
      showToast(`A folder may contain at most ${config.max_folder_files} files.`)
      return
    }
    if (mode === 'folder' && totalBytes > config.max_folder_total_size_mb * 1024 * 1024) {
      showToast(`Folder size may not exceed ${config.max_folder_total_size_mb} MB.`)
      return
    }

    setUploading(true)
    cancelledRef.current = false
    let collectionId: number | undefined
    let activeBatchId: number | undefined
    try {
      if (mode === 'folder') {
        if (collectionChoice === 'new') {
          const name = newCollectionName.trim() || folderName
          if (!name) throw new Error('Enter a collection name.')
          collectionId = (await createCollection(name)).id
        } else collectionId = Number(collectionChoice)
        const batch = await createUploadBatch(collectionId, folderName || newCollectionName.trim(), batchItems.length, totalBytes)
        activeBatchId = batch.id
        setBatchId(batch.id)
        const skipped = includeSkipped ? items.filter(item => item.status === 'skipped').length : 0
        if (skipped) await skipUploadBatchFiles(batch.id, skipped)
      }

      let cursor = 0
      const worker = async () => {
        while (!cancelledRef.current) {
          const index = cursor++
          const item = targets[index]
          if (!item) return
          const controller = new AbortController()
          controllersRef.current.set(item.id, controller)
          setItems(previous => previous.map(current => current.id === item.id ? { ...current, status: 'uploading', error: undefined } : current))
          const processingTimer = window.setTimeout(() => {
            setItems(previous => previous.map(current => current.id === item.id && current.status === 'uploading' ? { ...current, status: 'processing' } : current))
          }, 250)
          try {
            if (extension(item.file) === '.zip' && mode === 'files') {
              const archiveResult = await uploadZipArchive(item.file, {
                collectionId,
                signal: controller.signal,
                idempotencyKey: `archive:${item.idempotencyKey}`,
              })
              setItems(previous => previous.map(current => current.id === item.id ? { ...current, status: 'completed', archiveResult } : current))
            } else {
              const result = await uploadDocument(item.file, {
                collectionId, batchId: activeBatchId,
                relativePath: mode === 'folder' ? item.relativePath : undefined,
                signal: controller.signal,
                idempotencyKey: `document:${item.idempotencyKey}`,
              })
              setItems(previous => previous.map(current => current.id === item.id ? { ...current, status: 'completed', result } : current))
            }
          } catch (error) {
            const aborted = error instanceof DOMException && error.name === 'AbortError'
            const message = error instanceof ApiError ? error.message : aborted ? 'Cancelled' : error instanceof Error ? error.message : 'Upload failed.'
            setItems(previous => previous.map(current => current.id === item.id ? {
              ...current,
              status: aborted ? 'cancelled' : error instanceof ApiError && error.status === 409 ? 'skipped' : 'failed',
              error: message,
              retryable: error instanceof ApiError ? error.retryable : undefined,
            } : current))
          } finally {
            window.clearTimeout(processingTimer)
            controllersRef.current.delete(item.id)
          }
        }
      }
      await Promise.all(Array.from({ length: Math.min(config.max_concurrent_uploads, targets.length) }, () => worker()))
      await refreshDocuments()
    } catch (error) {
      showToast(error instanceof Error ? error.message : 'Folder upload could not be started.')
    } finally {
      setUploading(false)
    }
  }

  const cancel = async () => {
    cancelledRef.current = true
    controllersRef.current.forEach(controller => controller.abort())
    setItems(previous => previous.map(item => item.status === 'pending' ? { ...item, status: 'cancelled', error: 'Cancelled' } : item))
    if (batchId !== null) await cancelUploadBatch(batchId).catch(() => undefined)
    setUploading(false)
  }

  const close = () => {
    if (uploading) return
    setItems([])
    setFolderName('')
    setBatchId(null)
    onClose()
  }

  const retryFailed = () => {
    const failed = items.filter(item => item.status === 'failed' && item.retryable !== false)
    setItems(previous => previous.map(item => item.status === 'failed' && item.retryable !== false ? { ...item, status: 'pending', error: undefined, retryable: undefined } : item))
    window.setTimeout(() => void runUploads(failed.map(item => ({ ...item, status: 'pending' })), false), 0)
  }

  const readyItems = items.filter(item => item.status === 'pending' && (item.validation === 'Ready' || item.validation === 'Duplicate candidate'))
  const percent = items.length ? Math.round(summary.processed / items.length * 100) : 0

  return (<>
    <Modal open={open} onClose={close} title="Upload documents">
      <div className="grid grid-cols-2 gap-2 rounded-xl bg-slate-100 p-1">
        <button type="button" onClick={() => { setMode('files'); setItems([]) }} className={`rounded-lg px-3 py-2 text-xs font-semibold ${mode === 'files' ? 'bg-white text-blue-600 shadow-sm' : 'text-slate-500'}`}>Upload files</button>
        <button type="button" onClick={() => { setMode('folder'); setItems([]) }} className={`rounded-lg px-3 py-2 text-xs font-semibold ${mode === 'folder' ? 'bg-white text-blue-600 shadow-sm' : 'text-slate-500'}`}>Upload folder</button>
      </div>

      <div className="mt-4 rounded-2xl border border-dashed border-[#d9e3f1] bg-[#f8fbff] p-5 text-center">
        {mode === 'folder' ? <FolderOpen className="mx-auto text-blue-600" size={28} /> : <UploadCloud className="mx-auto text-blue-600" size={28} />}
        <p className="mt-2 text-sm font-semibold">Choose {mode === 'folder' ? 'a folder' : 'documents'} to preview</p>
        <Button className="mt-3" onClick={() => (mode === 'folder' ? folderInputRef : fileInputRef).current?.click()}>Browse {mode === 'folder' ? 'folder' : 'files'}</Button>
        <input ref={fileInputRef} type="file" multiple accept={[...config.supported_extensions, ...config.archive_extensions].join(',')} className="hidden" onChange={event => { selectFiles(Array.from(event.target.files ?? []), false); event.target.value = '' }} />
        <input ref={folderInputRef} type="file" multiple className="hidden" {...folderAttributes} onChange={onFolderSelection} />
      </div>

      {mode === 'folder' && items.length > 0 && (
        <div className="mt-4 grid gap-2 sm:grid-cols-2">
          <select value={collectionChoice} onChange={event => setCollectionChoice(event.target.value)} className="field h-10">
            <option value="new">Create new collection</option>
            {collections.map(collection => <option key={collection.id} value={collection.id}>{collection.name}</option>)}
          </select>
          {collectionChoice === 'new' && <input value={newCollectionName} onChange={event => setNewCollectionName(event.target.value)} className="field h-10" placeholder="Collection name" />}
        </div>
      )}

      {items.length > 0 && (
        <>
          <div className="mt-4 flex flex-wrap items-center justify-between gap-2 text-xs">
            <div><p className="font-semibold text-slate-800">{mode === 'folder' ? `Folder: ${folderName}` : `${items.length} files selected`}</p><p className="text-slate-500">{items.length} selected · {readyItems.length} ready · {items.filter(item => item.status === 'skipped').length} skipped</p></div>
            {(uploading || summary.processed > 0) && <div className="text-right"><p className="font-semibold">{summary.processed} of {items.length} processed</p><p className="text-blue-600">{percent}%</p></div>}
          </div>
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-100"><div className="h-full rounded-full bg-blue-600 transition-all" style={{ width: `${percent}%` }} /></div>

          <div className="mt-3 max-h-64 overflow-auto rounded-xl border border-slate-200">
            <table className="w-full min-w-[680px] text-left text-[10px]">
              <thead className="sticky top-0 bg-slate-50 text-slate-500"><tr><th className="p-2">File</th><th className="p-2">Relative path</th><th className="p-2">Type</th><th className="p-2">Size</th><th className="p-2">Validation</th><th className="p-2">Status</th></tr></thead>
              <tbody>{items.map(item => <tr key={item.id} className="border-t border-slate-100"><td className="max-w-36 p-2 font-semibold"><button type="button" onClick={() => setPreviewFile(item.file)} className="block max-w-36 truncate text-left text-blue-700 hover:underline" title={`Preview ${item.file.name}`}>{item.file.name}</button></td><td className="max-w-52 truncate p-2 text-slate-500">{item.relativePath}</td><td className="p-2 uppercase">{extension(item.file).slice(1) || '-'}</td><td className="p-2">{formatSize(item.file.size)}</td><td className="p-2">{item.validation}</td><td className="p-2"><span className={item.status === 'completed' ? 'text-emerald-600' : item.status === 'failed' ? 'text-red-600' : 'text-slate-600'}>{item.result?.content_reused ? 'restored' : item.status}</span>{item.error && <span className="block max-w-44 truncate text-red-500" title={item.error}>{item.error}</span>}</td></tr>)}</tbody>
            </table>
          </div>

          {items.some(item => item.archiveResult) && <div className="mt-3 space-y-2">{items.filter(item => item.archiveResult).map(item => {
            const archive = item.archiveResult!
            return <div key={`${item.id}-archive`} className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs">
              <p className="font-semibold text-slate-800">{archive.archive}: {archive.status.replace('_', ' ')}</p>
              <p className="mt-1 text-slate-500">{archive.summary.uploaded} uploaded · {archive.summary.duplicates} duplicate/reused · {archive.summary.failed} failed</p>
              <div className="mt-2 max-h-28 space-y-1 overflow-auto">{archive.files.map((file, index) => <div key={`${file.filename}-${index}`} className="flex justify-between gap-3"><span className="truncate">{file.filename}</span><span className={file.status === 'uploaded' || file.status === 'duplicate_content_reused' ? 'text-emerald-600' : file.status === 'duplicate' ? 'text-slate-600' : 'text-red-600'} title={file.reason}>{file.status.replace(/_/g, ' ')}</span></div>)}</div>
            </div>
          })}</div>}

          {!uploading && summary.processed === items.length && <div className="mt-3 rounded-xl bg-slate-50 p-3 text-xs"><p className="font-semibold">Upload summary</p><div className="mt-2 flex flex-wrap gap-4"><span className="text-emerald-700"><CheckCircle2 className="mr-1 inline" size={14} />{summary.completed} uploaded</span><span>{summary.duplicates} already existed/reused</span><span>{summary.skipped} skipped</span><span className="text-red-700"><AlertCircle className="mr-1 inline" size={14} />{summary.failed} failed</span></div></div>}
        </>
      )}

      <div className="mt-5 flex flex-wrap justify-end gap-2">
        {uploading ? <Button variant="secondary" onClick={() => void cancel()}><X size={15} />Cancel upload</Button> : <Button variant="secondary" onClick={close}>Close</Button>}
        {!uploading && items.some(item => item.status === 'failed' && item.retryable !== false) && <Button variant="secondary" onClick={retryFailed}><RotateCcw size={15} />Retry failed</Button>}
        <Button onClick={() => void runUploads(readyItems)} disabled={!readyItems.length || uploading}>{uploading ? 'Processing...' : mode === 'folder' ? 'Upload folder' : 'Upload files'}</Button>
      </div>
    </Modal>
    <DocumentPreviewModal file={previewFile} open={previewFile !== null} onClose={() => setPreviewFile(null)} />
  </>)
}
