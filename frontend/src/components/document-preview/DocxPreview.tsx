import DOMPurify from 'dompurify'
import { useEffect, useState } from 'react'

export default function DocxPreview({ blob }: { blob: Blob }) {
  const [html, setHtml] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const mammoth = await import('mammoth')
        const result = await mammoth.convertToHtml({ arrayBuffer: await blob.arrayBuffer() })
        if (!cancelled) setHtml(DOMPurify.sanitize(result.value, { USE_PROFILES: { html: true } }))
      } catch {
        if (!cancelled) setError('This Word document could not be rendered.')
      }
    })()
    return () => { cancelled = true }
  }, [blob])
  if (error) return <p className="p-8 text-center text-xs font-semibold text-red-600">{error}</p>
  if (!html) return <p className="p-8 text-center text-xs text-slate-500">Reading Word document…</p>
  return <div className="h-full overflow-auto bg-slate-100 p-4 sm:p-8"><article className="preview-document-page" dangerouslySetInnerHTML={{ __html: html }} /></div>
}
