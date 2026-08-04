import { describe, expect, it } from 'vitest'
import { formatFileSize, resolvePreviewKind, sourceFromFile } from './previewTypes'

describe('document preview resolver', () => {
  it.each([
    ['report.pdf', 'pdf'],
    ['photo.PNG', 'image'],
    ['records.xlsx', 'spreadsheet'],
    ['records.csv', 'spreadsheet'],
    ['policy.docx', 'docx'],
    ['notes.md', 'markdown'],
    ['data.json', 'json'],
    ['layout.xml', 'source'],
    ['page.html', 'source'],
    ['readme.txt', 'text'],
    ['slides.pptx', 'unsupported'],
  ])('maps %s to %s', (name, expected) => {
    expect(resolvePreviewKind({ name, mimeType: '' })).toBe(expected)
  })

  it('falls back to an image MIME type and describes local files', () => {
    expect(resolvePreviewKind({ name: 'scan', mimeType: 'image/png' })).toBe('image')
    const file = new File(['hello'], 'notes.txt', { type: 'text/plain' })
    expect(sourceFromFile(file)).toMatchObject({ name: 'notes.txt', type: 'TXT', file })
    expect(formatFileSize(5)).toBe('5 B')
  })
})
