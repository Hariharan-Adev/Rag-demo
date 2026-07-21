import { CheckCircle2, FileText, UploadCloud, X } from 'lucide-react'
import { useRef, useState, type DragEvent } from 'react'
import { motion } from 'framer-motion'
import { useApp } from '../context/AppContext'
import { ApiError, type UploadResponse } from '../services/api'
import { Button } from './ui/Button'
import { Modal } from './ui/Modal'

type UploadItem = {
  id: string
  file: File
  status: 'ready' | 'uploading' | 'done' | 'failed'
  result?: UploadResponse
  error?: string
}

const allowedExtensions = ['txt', 'pdf', 'docx', 'xlsx', 'xls', 'csv', 'pptx', 'ppt', 'png', 'jpg', 'jpeg', 'bmp', 'gif', 'tiff', 'webp']
const maxFileSize = 10 * 1024 * 1024

export default function UploadDocumentsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { uploadDocuments, showToast } = useApp()
  const [files, setFiles] = useState<UploadItem[]>([])
  const [uploading, setUploading] = useState(false)
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const accept = (incoming: File[]) => {
    const valid = incoming.filter(file => {
      const extension = file.name.split('.').pop()?.toLowerCase() ?? ''
      return allowedExtensions.includes(extension) && file.size <= maxFileSize
    })

    if (valid.length < incoming.length) {
      showToast('Choose a supported document, spreadsheet, presentation, or image up to 10 MB.')
    }

    setFiles(previous => [
      ...previous,
      ...valid.map(file => ({
        id: crypto.randomUUID(),
        file,
        status: 'ready' as const,
      })),
    ])
  }

  const drop = (event: DragEvent) => {
    event.preventDefault()
    setDragging(false)
    accept(Array.from(event.dataTransfer.files))
  }

  const upload = async () => {
    const readyFiles = files.filter(item => item.status === 'ready')
    if (!readyFiles.length || uploading) return

    setUploading(true)
    setFiles(previous => previous.map(item => (
      item.status === 'ready' ? { ...item, status: 'uploading' } : item
    )))

    for (const item of readyFiles) {
      try {
        const [result] = await uploadDocuments([item.file])
        setFiles(previous => previous.map(current => (
          current.id === item.id ? { ...current, status: 'done', result } : current
        )))
      } catch (error) {
        const message = error instanceof ApiError ? error.message : 'Upload failed.'
        setFiles(previous => previous.map(current => (
          current.id === item.id ? { ...current, status: 'failed', error: message } : current
        )))
      }
    }

    setUploading(false)
  }

  const close = () => {
    if (uploading) return
    setFiles([])
    onClose()
  }

  return (
    <Modal open={open} onClose={close} title="Upload documents">
      <div
        onDragOver={event => {
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={drop}
        className={`grid min-h-44 place-items-center rounded-2xl border border-dashed p-6 text-center transition ${dragging ? 'border-blue-500 bg-blue-50' : 'border-[#d9e3f1] bg-[#f8fbff]'}`}
      >
        <div>
          <motion.div animate={dragging ? { y: [0, -5, 0] } : {}} transition={{ repeat: dragging ? Infinity : 0, duration: 1 }} className="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-gradient-to-br from-blue-600 to-indigo-500 text-white shadow-[0_8px_20px_rgba(37,99,235,.22)]">
            <UploadCloud size={23} />
          </motion.div>
          <p className="mt-3 text-sm font-semibold">
            Drop files here or{' '}
            <button type="button" onClick={() => inputRef.current?.click()} className="text-blue-600 hover:underline">
              browse
            </button>
          </p>
          <div className="mt-2 grid grid-cols-2 gap-x-5 gap-y-1.5 text-left text-[10px] leading-4 text-slate-500">
            <p><span className="block font-semibold text-slate-700">Documents</span>TXT • PDF • DOCX</p>
            <p><span className="block font-semibold text-slate-700">Spreadsheets</span>XLSX • XLS • CSV</p>
            <p><span className="block font-semibold text-slate-700">Presentations</span>PPTX • PPT</p>
            <p><span className="block font-semibold text-slate-700">Images</span>PNG • JPG • JPEG • BMP • GIF • TIFF • WEBP</p>
          </div>
          <p className="mt-2 text-[10px] text-slate-500">Maximum 10 MB per file.</p>
          <input ref={inputRef} type="file" multiple accept=".txt,.pdf,.docx,.xlsx,.xls,.csv,.pptx,.ppt,.png,.jpg,.jpeg,.bmp,.gif,.tiff,.webp" className="hidden" onChange={event => accept(Array.from(event.target.files ?? []))} />
        </div>
      </div>

      <div className="mt-4 max-h-56 space-y-2 overflow-y-auto">
        {files.map(item => (
          <div key={item.id} className="rounded-xl border border-[#eef2f7] bg-white p-3 shadow-[0_4px_16px_rgba(37,99,235,.04)]">
            <div className="flex items-center gap-3">
              <span className="grid h-9 w-9 place-items-center rounded-lg bg-red-50 text-red-500">
                {item.status === 'done' ? <CheckCircle2 className="text-emerald-500" size={18} /> : <FileText size={17} />}
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-xs font-semibold">{item.file.name}</p>
                <p className="text-[9px] text-slate-400">{(item.file.size / 1024 / 1024).toFixed(2)} MB</p>
              </div>
              {item.status !== 'uploading' && (
                <button onClick={() => setFiles(previous => previous.filter(file => file.id !== item.id))} className="text-slate-400 hover:text-red-500" aria-label={`Remove ${item.file.name}`}>
                  <X size={15} />
                </button>
              )}
            </div>

            {item.status === 'uploading' && <div className="mt-2"><div className="h-1.5 overflow-hidden rounded-full bg-slate-100"><div className="h-full w-2/3 animate-pulse rounded-full bg-blue-600" /></div><p className="mt-1.5 text-[10px] font-medium text-blue-600">Processing and indexing...</p></div>}
            {item.status === 'done' && item.result && (
              <p className="mt-2 text-[10px] font-semibold text-emerald-700">
                Document #{item.result.document_id} indexed with {item.result.chunk_count} chunks.
              </p>
            )}
            {item.status === 'failed' && <p className="mt-2 text-[10px] font-semibold text-red-700">{item.error}</p>}
          </div>
        ))}
      </div>

      <div className="mt-5 flex justify-end gap-2">
        <Button variant="secondary" onClick={close} disabled={uploading}>Close</Button>
        <Button onClick={upload} disabled={!files.some(item => item.status === 'ready') || uploading}>
          {uploading ? 'Processing...' : 'Upload files'}
        </Button>
      </div>
    </Modal>
  )
}
