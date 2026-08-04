import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export default function TextPreview({ blob, kind }: { blob: Blob; kind: 'markdown' | 'text' | 'json' | 'source' }) {
  const [text, setText] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    let cancelled = false
    void blob.text().then(value => {
      if (cancelled) return
      if (kind === 'json') {
        try { setText(JSON.stringify(JSON.parse(value), null, 2)) } catch { setText(value) }
      } else setText(value)
    }).catch(() => { if (!cancelled) setError('This text file could not be read.') })
    return () => { cancelled = true }
  }, [blob, kind])
  if (error) return <p className="p-8 text-center text-xs font-semibold text-red-600">{error}</p>
  if (kind === 'markdown') return <div className="h-full overflow-auto bg-slate-100 p-4 sm:p-8"><article className="preview-document-page markdown-content"><ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown></article></div>
  return <pre className="h-full overflow-auto whitespace-pre-wrap break-words bg-slate-950 p-5 font-mono text-xs leading-6 text-slate-100">{text}</pre>
}
