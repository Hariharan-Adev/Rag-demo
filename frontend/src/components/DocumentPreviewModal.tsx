import { FileText, Highlighter } from 'lucide-react'
import { useApp } from '../context/AppContext'
import { Modal } from './ui/Modal'

export default function DocumentPreviewModal() {
  const { selectedDocument, setSelectedDocument, retrievedDocuments } = useApp()
  if (!selectedDocument) return null

  const reference = retrievedDocuments.find(source => source.id === selectedDocument.id || source.name === selectedDocument.name)

  return (
    <Modal open onClose={() => setSelectedDocument(null)} title={selectedDocument.name}>
      <div className="flex items-center justify-between rounded-2xl border border-[#eef2f7] bg-[#f8fbff] p-3 shadow-[0_4px_16px_rgba(37,99,235,.04)]">
        <div className="flex items-center gap-3">
          <span className="grid h-10 w-10 place-items-center rounded-xl bg-gradient-to-br from-blue-600 to-indigo-500 text-white shadow-[0_5px_14px_rgba(37,99,235,.18)]"><FileText size={19} /></span>
          <div>
            <p className="text-xs font-semibold">{selectedDocument.type} - {selectedDocument.size}</p>
            <p className="text-[10px] text-slate-500">Indexed {selectedDocument.updatedAt}</p>
          </div>
        </div>
      </div>
      <div className="relative mt-4 overflow-hidden rounded-2xl border border-[#eef2f7] bg-[#f8fbff] p-4">
        <div className="rounded-xl border border-dashed border-[#d9e3f1] bg-white p-5 text-sm leading-6 text-slate-600 shadow-sm">
          Full document text stays server-side. The UI displays only metadata and retrieved source names.
        </div>
        {reference && (
          <div className="mt-4 flex items-center gap-2 rounded-lg border border-yellow-200 bg-yellow-50 p-3 text-[11px] font-semibold text-yellow-800">
            <Highlighter size={14} /> Referenced by the latest answer with {reference.score}% similarity.
          </div>
        )}
      </div>
    </Modal>
  )
}
