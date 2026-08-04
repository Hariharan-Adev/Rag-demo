import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AppProvider } from '../../context/AppContext'
import DocumentPreviewModal from '../DocumentPreviewModal'

vi.mock('../../services/api', () => ({
  ApiError: class ApiError extends Error { status = 500 },
  deleteDocument: vi.fn(),
  fetchDocumentContent: vi.fn(),
  listCollections: vi.fn().mockResolvedValue({ collections: [] }),
  listDocuments: vi.fn().mockResolvedValue({ documents: [] }),
  listDocumentVersions: vi.fn().mockResolvedValue({ versions: [] }),
  makeDocumentVersionCurrent: vi.fn(),
  sendChatMessage: vi.fn(),
  uploadDocument: vi.fn(),
  uploadDocumentVersion: vi.fn(),
}))

describe('DocumentPreviewModal accessibility and local lifecycle', () => {
  const createDescriptor = Object.getOwnPropertyDescriptor(URL, 'createObjectURL')
  const revokeDescriptor = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL')
  beforeEach(() => {
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:local-preview') })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
  })
  afterEach(() => {
    if (createDescriptor) Object.defineProperty(URL, 'createObjectURL', createDescriptor)
    else delete (URL as { createObjectURL?: typeof URL.createObjectURL }).createObjectURL
    if (revokeDescriptor) Object.defineProperty(URL, 'revokeObjectURL', revokeDescriptor)
    else delete (URL as { revokeObjectURL?: typeof URL.revokeObjectURL }).revokeObjectURL
  })

  function renderModal(onClose = vi.fn()) {
    const file = new File(['local preview'], 'notes.txt', { type: 'text/plain' })
    render(<AppProvider userEmail="test@example.com" onLogout={vi.fn()}><DocumentPreviewModal file={file} open onClose={onClose} /></AppProvider>)
    return onClose
  }

  it('uses a labelled modal, locks body scrolling, and closes with Escape', async () => {
    const onClose = renderModal()
    const dialog = await screen.findByRole('dialog', { name: 'notes.txt' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(document.body.style.overflow).toBe('hidden')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('closes only from the backdrop and revokes its object URL on unmount', async () => {
    const onClose = vi.fn()
    const view = render(<AppProvider userEmail="test@example.com" onLogout={vi.fn()}><DocumentPreviewModal file={new File(['x'], 'x.txt')} open onClose={onClose} /></AppProvider>)
    const dialog = await screen.findByRole('dialog', { name: 'x.txt' })
    fireEvent.mouseDown(dialog)
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.mouseDown(dialog.parentElement!)
    expect(onClose).toHaveBeenCalledOnce()
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalled())
    view.unmount()
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:local-preview'))
  })
})
