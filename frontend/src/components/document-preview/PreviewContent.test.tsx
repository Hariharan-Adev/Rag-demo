import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import PreviewContent from './PreviewContent'
import TextPreview from './TextPreview'

describe('PreviewContent', () => {
  it('shows a safe fallback for unsupported formats', () => {
    render(<PreviewContent source={{ name: 'deck.pptx', type: 'PPTX', size: '1 MB' }} blob={new Blob()} objectUrl="blob:test" />)
    expect(screen.getByText('Preview unavailable')).toBeInTheDocument()
    expect(screen.getByText(/still download it/i)).toBeInTheDocument()
  })

  it('renders plain text through the lazy text viewer', async () => {
    render(<TextPreview blob={new Blob(['hello'], { type: 'text/plain' })} kind="text" />)
    expect(await screen.findByText('hello')).toBeInTheDocument()
  })
})
