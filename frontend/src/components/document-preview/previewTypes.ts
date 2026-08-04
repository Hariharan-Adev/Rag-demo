import type { PolicyDocument } from '../../types'

export type PreviewKind = 'pdf' | 'image' | 'spreadsheet' | 'docx' | 'markdown' | 'text' | 'json' | 'source' | 'unsupported'

export interface PreviewSource {
  name: string
  type: string
  size: string
  mimeType?: string | null
  documentId?: string
  file?: File
}

const imageExtensions = new Set(['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'tiff'])
const spreadsheetExtensions = new Set(['xlsx', 'xls', 'csv'])

export function extensionOf(name: string) {
  const index = name.lastIndexOf('.')
  return index < 0 ? '' : name.slice(index + 1).toLowerCase()
}

export function resolvePreviewKind(source: Pick<PreviewSource, 'name' | 'mimeType'>): PreviewKind {
  const extension = extensionOf(source.name)
  const mime = source.mimeType?.toLowerCase() ?? ''
  if (extension === 'pdf' || mime === 'application/pdf') return 'pdf'
  if (imageExtensions.has(extension) || mime.startsWith('image/')) return 'image'
  if (spreadsheetExtensions.has(extension)) return 'spreadsheet'
  if (extension === 'docx') return 'docx'
  if (extension === 'md' || extension === 'markdown') return 'markdown'
  if (extension === 'json' || mime === 'application/json') return 'json'
  if (['xml', 'html', 'htm'].includes(extension)) return 'source'
  if (['txt', 'log'].includes(extension) || mime.startsWith('text/')) return 'text'
  return 'unsupported'
}

export function sourceFromDocument(document: PolicyDocument): PreviewSource {
  return {
    name: document.name,
    type: document.type,
    size: document.size,
    mimeType: document.mimeType,
    documentId: document.id,
  }
}

export function formatFileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

export function sourceFromFile(file: File): PreviewSource {
  const extension = extensionOf(file.name)
  return {
    name: file.name,
    type: extension.toUpperCase() || 'FILE',
    size: formatFileSize(file.size),
    mimeType: file.type,
    file,
  }
}
