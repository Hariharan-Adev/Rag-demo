export type Theme = 'light' | 'dark'
export type View = 'chat' | 'dashboard' | 'library'

export interface User {
  id: string
  name: string
  role: string
  initials: string
}

export interface PolicyDocument {
  id: string
  name: string
  type: 'TXT' | 'PDF' | 'DOCX' | 'XLSX' | 'XLS' | 'CSV' | 'PPTX' | 'PPT' | 'PNG' | 'JPG' | 'JPEG' | 'BMP' | 'GIF' | 'TIFF' | 'WEBP'
  size: string
  chunks: number
  category: string
  updatedAt: string
  uploaded?: boolean
  collectionId?: number | null
  collectionName?: string | null
  relativePath?: string | null
  visibility?: 'private' | 'organization'
  processingStatus?: string
  currentVersionId?: number | null
  currentVersionNumber?: number | null
}

export interface RetrievedDocument {
  id: string
  name: string
  section: string
  score: number
  category: string
}

export interface ResponseMetadata {
  embeddingModel: string
  llmModel: string
  chunksRetrieved: number
  latency: string
  timestamp: string
}

export interface ChatItem {
  id: number
  role: 'user' | 'assistant'
  content: string
  detail?: string
  source?: RetrievedDocument
  liked?: boolean
  disliked?: boolean
  bookmarked?: boolean
}

export interface Conversation {
  id: string
  title: string
  createdAt: string
  updatedAt: string
  messages: ChatItem[]
  isPinned: boolean
  pinnedAt?: string | null
  selectedDocumentIds?: string[]
}

export interface NotificationItem {
  id: string
  title: string
  description: string
  time: string
  read: boolean
  tone: 'blue' | 'green' | 'purple'
}

