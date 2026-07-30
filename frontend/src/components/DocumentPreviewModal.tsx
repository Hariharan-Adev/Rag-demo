import { CheckCircle2, Clock3, FileText, Highlighter, RotateCcw, XCircle } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useApp } from '../context/AppContext'
import { listDocumentVersions, makeDocumentVersionCurrent, uploadDocumentVersion, type DocumentVersion } from '../services/api'
import { Button } from './ui/Button'
import { Modal } from './ui/Modal'

export default function DocumentPreviewModal() {
  const { selectedDocument, setSelectedDocument, retrievedDocuments, refreshDocuments, showToast } = useApp()
  const [versions, setVersions] = useState<DocumentVersion[]>([])
  const [loadingVersions, setLoadingVersions] = useState(false)
  const [versionError, setVersionError] = useState('')
  const [uploadingVersion, setUploadingVersion] = useState(false)

  useEffect(() => {
    if (!selectedDocument?.uploaded) return
    let active = true
    setLoadingVersions(true)
    setVersionError('')
    void listDocumentVersions(selectedDocument.id)
      .then(result => { if (active) setVersions(result.versions) })
      .catch(error => { if (active) setVersionError(error instanceof Error ? error.message : 'Unable to load version history.') })
      .finally(() => { if (active) setLoadingVersions(false) })
    return () => { active = false }
  }, [selectedDocument?.id, selectedDocument?.uploaded])

  if (!selectedDocument) return null
  const reference = retrievedDocuments.find(source => source.id === selectedDocument.id || source.name === selectedDocument.name)

  const makeCurrent = async (version: DocumentVersion) => {
    await makeDocumentVersionCurrent(selectedDocument.id, version.id)
    setVersions(previous => previous.map(item => ({ ...item, is_current: item.id === version.id })))
    await refreshDocuments()
    showToast(`Version ${version.version_number} is now current`)
  }

  const addVersion = async (file: File) => {
    setUploadingVersion(true)
    setVersionError('')
    try {
      await uploadDocumentVersion(selectedDocument.id, file)
      const result = await listDocumentVersions(selectedDocument.id)
      setVersions(result.versions)
      await refreshDocuments()
      showToast('New document version processed')
    } catch (error) {
      setVersionError(error instanceof Error ? error.message : 'Version upload failed.')
    } finally {
      setUploadingVersion(false)
    }
  }

  return (
    <Modal open onClose={() => setSelectedDocument(null)} title={selectedDocument.name}>
      <div className="flex items-center justify-between rounded-2xl border border-[#eef2f7] bg-[#f8fbff] p-3 shadow-[0_4px_16px_rgba(37,99,235,.04)]">
        <div className="flex items-center gap-3">
          <span className="grid h-10 w-10 place-items-center rounded-xl bg-gradient-to-br from-blue-600 to-indigo-500 text-white shadow-[0_5px_14px_rgba(37,99,235,.18)]"><FileText size={19} /></span>
          <div>
            <p className="text-xs font-semibold">{selectedDocument.type} · {selectedDocument.size}</p>
            <p className="text-[10px] text-slate-500">Current version {selectedDocument.currentVersionNumber ?? '—'} · {selectedDocument.visibility ?? 'private'}</p>
          </div>
        </div>
      </div>
      {reference && (
        <div className="mt-4 flex items-center gap-2 rounded-lg border border-yellow-200 bg-yellow-50 p-3 text-[11px] font-semibold text-yellow-800">
          <Highlighter size={14} /> Selected by semantic retrieval. Ranking signals are not factual confidence.
        </div>
      )}
      <div className="mt-4">
        <div className="flex items-center justify-between"><p className="text-xs font-semibold text-slate-700">Version history</p><label className="cursor-pointer rounded-lg bg-blue-50 px-3 py-1.5 text-[10px] font-semibold text-blue-700 hover:bg-blue-100">{uploadingVersion ? 'Processing…' : 'Upload new version'}<input type="file" className="hidden" disabled={uploadingVersion} onChange={event => { const file = event.target.files?.[0]; if (file) void addVersion(file); event.currentTarget.value = '' }} /></label></div>
        {loadingVersions && <p className="mt-2 text-xs text-slate-400">Loading versions…</p>}
        {versionError && <p className="mt-2 text-xs text-red-600">{versionError}</p>}
        <div className="mt-2 space-y-2">
          {versions.map(version => (
            <div key={version.id} className="flex items-center gap-3 rounded-xl border border-slate-200 p-3 text-xs">
              {version.status === 'completed' ? <CheckCircle2 size={15} className="text-emerald-600" /> : version.status === 'failed' ? <XCircle size={15} className="text-red-600" /> : <Clock3 size={15} className="text-blue-600" />}
              <div className="min-w-0 flex-1">
                <p className="font-semibold">Version {version.version_number}{version.is_current ? ' · Current' : ''}</p>
                <p className="truncate text-[10px] text-slate-500">{version.error?.message ?? version.status}</p>
              </div>
              {!version.is_current && version.status === 'completed' && <Button variant="secondary" onClick={() => void makeCurrent(version)}><RotateCcw size={13} />Use</Button>}
            </div>
          ))}
        </div>
      </div>
    </Modal>
  )
}
