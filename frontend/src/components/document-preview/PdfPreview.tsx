import { ChevronLeft, ChevronRight, Maximize2, Minus, Plus } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Document, Page, pdfjs } from 'react-pdf'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'

pdfjs.GlobalWorkerOptions.workerSrc = new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url).toString()

export default function PdfPreview({ objectUrl }: { objectUrl: string }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [pages, setPages] = useState(0)
  const [page, setPage] = useState(1)
  const [zoom, setZoom] = useState(1)
  const [width, setWidth] = useState(720)

  useEffect(() => {
    const element = containerRef.current
    if (!element) return
    const update = () => setWidth(Math.max(260, element.clientWidth - 40))
    update()
    const observer = new ResizeObserver(update)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  return <div className="flex h-full min-h-0 flex-col">
    <div className="preview-toolbar" aria-label="PDF controls">
      <button type="button" onClick={() => setPage(value => Math.max(1, value - 1))} disabled={page <= 1} aria-label="Previous page"><ChevronLeft size={16} /></button>
      <span>Page <strong>{page}</strong> / {pages || '—'}</span>
      <button type="button" onClick={() => setPage(value => Math.min(pages, value + 1))} disabled={!pages || page >= pages} aria-label="Next page"><ChevronRight size={16} /></button>
      <span className="preview-toolbar__divider" />
      <button type="button" onClick={() => setZoom(value => Math.max(.5, value - .1))} aria-label="Zoom out"><Minus size={15} /></button>
      <span>{Math.round(zoom * 100)}%</span>
      <button type="button" onClick={() => setZoom(value => Math.min(2.5, value + .1))} aria-label="Zoom in"><Plus size={15} /></button>
      <button type="button" onClick={() => setZoom(1)} aria-label="Fit page width"><Maximize2 size={15} /><span>Fit</span></button>
    </div>
    <div ref={containerRef} className="min-h-0 flex-1 overflow-auto bg-slate-200/70 p-5">
      <Document file={objectUrl} loading={<p className="p-8 text-center text-xs text-slate-500">Loading PDF…</p>} error={<p className="p-8 text-center text-xs font-semibold text-red-600">This PDF could not be rendered.</p>} onLoadSuccess={({ numPages }) => { setPages(numPages); setPage(current => Math.min(current, numPages)) }}>
        <Page pageNumber={page} width={width * zoom} className="mx-auto w-fit shadow-xl" renderTextLayer renderAnnotationLayer />
      </Document>
    </div>
  </div>
}
