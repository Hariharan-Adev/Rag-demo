import { lazy, Suspense } from 'react'
import { FileQuestion } from 'lucide-react'
import { resolvePreviewKind, type PreviewSource } from './previewTypes'

const PdfPreview = lazy(() => import('./PdfPreview'))
const ImagePreview = lazy(() => import('./ImagePreview'))
const SpreadsheetPreview = lazy(() => import('./SpreadsheetPreview'))
const DocxPreview = lazy(() => import('./DocxPreview'))
const TextPreview = lazy(() => import('./TextPreview'))

export default function PreviewContent({ source, blob, objectUrl }: { source: PreviewSource; blob: Blob; objectUrl: string }) {
  const kind = resolvePreviewKind(source)
  let content
  if (kind === 'pdf') content = <PdfPreview objectUrl={objectUrl} />
  else if (kind === 'image') content = <ImagePreview name={source.name} objectUrl={objectUrl} />
  else if (kind === 'spreadsheet') content = <SpreadsheetPreview blob={blob} />
  else if (kind === 'docx') content = <DocxPreview blob={blob} />
  else if (['markdown', 'text', 'json', 'source'].includes(kind)) content = <TextPreview blob={blob} kind={kind as 'markdown' | 'text' | 'json' | 'source'} />
  else content = <UnsupportedPreview />

  return <Suspense fallback={<PreviewLoading />}>{content}</Suspense>
}

export function PreviewLoading() {
  return <div className="grid h-full min-h-64 place-items-center" role="status"><div className="text-center"><span className="mx-auto block h-8 w-8 animate-spin rounded-full border-2 border-blue-100 border-t-blue-600" /><p className="mt-3 text-xs font-semibold text-slate-500">Preparing preview…</p></div></div>
}

function UnsupportedPreview() {
  return <div className="grid h-full min-h-64 place-items-center px-6 text-center"><div><FileQuestion className="mx-auto text-slate-300" size={42} /><h3 className="mt-3 text-sm font-bold text-slate-700">Preview unavailable</h3><p className="mt-1 max-w-sm text-xs leading-5 text-slate-500">This file type cannot be rendered safely in the browser. You can still download it or open it in a compatible application.</p></div></div>
}
