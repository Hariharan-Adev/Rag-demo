import { Maximize2, Minus, Plus } from 'lucide-react'
import { useRef, useState, type PointerEvent } from 'react'

export default function ImagePreview({ name, objectUrl }: { name: string; objectUrl: string }) {
  const [zoom, setZoom] = useState(1)
  const [fit, setFit] = useState(true)
  const containerRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ x: number; y: number; left: number; top: number } | null>(null)

  const startPan = (event: PointerEvent<HTMLDivElement>) => {
    const element = containerRef.current
    if (!element || fit) return
    element.setPointerCapture(event.pointerId)
    dragRef.current = { x: event.clientX, y: event.clientY, left: element.scrollLeft, top: element.scrollTop }
  }
  const pan = (event: PointerEvent<HTMLDivElement>) => {
    const element = containerRef.current
    const start = dragRef.current
    if (!element || !start) return
    element.scrollLeft = start.left - (event.clientX - start.x)
    element.scrollTop = start.top - (event.clientY - start.y)
  }

  return <div className="flex h-full min-h-0 flex-col">
    <div className="preview-toolbar" aria-label="Image controls">
      <button type="button" onClick={() => { setFit(false); setZoom(value => Math.max(.25, value - .25)) }} aria-label="Zoom out"><Minus size={15} /></button>
      <span>{fit ? 'Fit' : `${Math.round(zoom * 100)}%`}</span>
      <button type="button" onClick={() => { setFit(false); setZoom(value => Math.min(5, value + .25)) }} aria-label="Zoom in"><Plus size={15} /></button>
      <button type="button" onClick={() => { setFit(true); setZoom(1) }} aria-label="Fit image"><Maximize2 size={15} /><span>Fit</span></button>
    </div>
    <div ref={containerRef} onPointerDown={startPan} onPointerMove={pan} onPointerUp={() => { dragRef.current = null }} className={`grid min-h-0 flex-1 overflow-auto bg-[linear-gradient(45deg,#f1f5f9_25%,transparent_25%),linear-gradient(-45deg,#f1f5f9_25%,transparent_25%),linear-gradient(45deg,transparent_75%,#f1f5f9_75%),linear-gradient(-45deg,transparent_75%,#f1f5f9_75%)] bg-[length:20px_20px] bg-[position:0_0,0_10px,10px_-10px,-10px_0px] p-5 ${fit ? 'place-items-center' : 'cursor-grab active:cursor-grabbing'}`}>
      <img src={objectUrl} alt={`Preview of ${name}`} draggable={false} className={fit ? 'max-h-full max-w-full object-contain' : 'max-w-none select-none'} style={fit ? undefined : { width: `${zoom * 100}%` }} />
    </div>
  </div>
}
