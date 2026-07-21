import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { ChatItem, Conversation, NotificationItem, PolicyDocument, ResponseMetadata, RetrievedDocument, Theme, User, View } from '../types'
import { ApiError, deleteDocument, listDocuments, sendChatMessage, uploadDocument, type ChatSource, type DocumentRecord, type UploadResponse } from '../services/api'

const defaultSuggestions = [
  'What is this document about?',
  'Summarize the uploaded document.',
  'What are the key facts in my document?',
]

interface AppContextValue {
  user: User
  messages: ChatItem[]
  conversations: Conversation[]
  activeConversationId: string
  documents: PolicyDocument[]
  selectedCategory: string
  retrievedDocuments: RetrievedDocument[]
  suggestions: string[]
  confidence: number
  metadata: ResponseMetadata | null
  theme: Theme
  notifications: NotificationItem[]
  sidebarOpen: boolean
  loading: boolean
  bookmarks: ChatItem[]
  recentQuestions: string[]
  view: View
  toast: string
  selectedDocument: PolicyDocument | null
  setSelectedCategory: (category: string) => void
  setSidebarOpen: (open: boolean) => void
  setView: (view: View) => void
  setSelectedDocument: (doc: PolicyDocument | null) => void
  showToast: (message: string) => void
  newChat: () => void
  selectConversation: (id: string) => void
  renameConversation: (id: string, title: string) => void
  deleteConversation: (id: string) => void
  toggleConversationPin: (id: string) => void
  sendMessage: (question: string) => Promise<void>
  clearChat: () => void
  uploadDocuments: (files: File[]) => Promise<UploadResponse[]>
  removeDocument: (id: string) => Promise<void>
  toggleTheme: () => void
  markNotificationsRead: () => void
  updateMessage: (id: number, patch: Partial<ChatItem>) => void
  regenerate: (id: number) => void
  clearHistory: () => void
  logout: () => void
}

interface AppProviderProps {
  children: ReactNode
  userEmail: string
  onLogout: () => void
}

const AppContext = createContext<AppContextValue | null>(null)
const CHAT_HISTORY_VERSION = 'simple-rag-chat-history-v1'

function conversationStorageKey(email: string) {
  return `${CHAT_HISTORY_VERSION}:${email.toLowerCase()}`
}

function createConversationId() {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `chat-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function createConversationTitle(question: string) {
  const normalized = question.replace(/\s+/g, ' ').trim()
  return normalized.length > 48 ? `${normalized.slice(0, 47).trimEnd()}…` : normalized
}

function isChatItem(value: unknown): value is ChatItem {
  if (!value || typeof value !== 'object') return false
  const message = value as Partial<ChatItem>
  return typeof message.id === 'number' && (message.role === 'user' || message.role === 'assistant') && typeof message.content === 'string'
}

function readConversations(email: string): Conversation[] {
  try {
    const raw = localStorage.getItem(conversationStorageKey(email))
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter((value): value is Conversation => {
      if (!value || typeof value !== 'object') return false
      const conversation = value as Partial<Conversation>
      return typeof conversation.id === 'string'
        && typeof conversation.title === 'string'
        && typeof conversation.createdAt === 'string'
        && typeof conversation.updatedAt === 'string'
        && Array.isArray(conversation.messages)
        && conversation.messages.every(isChatItem)
    }).map(conversation => ({
      ...conversation,
      isPinned: conversation.isPinned === true,
      pinnedAt: conversation.isPinned === true && typeof conversation.pinnedAt === 'string' ? conversation.pinnedAt : null,
    }))
  } catch {
    return []
  }
}

function readTheme(): Theme {
  try {
    return localStorage.getItem('rag-theme') === '"dark"' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

function initialsFromEmail(email: string) {
  return email.slice(0, 2).toUpperCase()
}

function documentType(filename: string): PolicyDocument['type'] {
  const extension = filename.split('.').pop()?.toUpperCase()
  const supported: PolicyDocument['type'][] = ['TXT', 'PDF', 'DOCX', 'XLSX', 'XLS', 'CSV', 'PPTX', 'PPT', 'PNG', 'JPG', 'JPEG', 'BMP', 'GIF', 'TIFF', 'WEBP']
  return supported.includes(extension as PolicyDocument['type']) ? extension as PolicyDocument['type'] : 'TXT'
}

function formatDate(value: string) {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString()
}

function mapDocument(row: DocumentRecord): PolicyDocument {
  return {
    id: String(row.id),
    name: row.filename,
    type: documentType(row.filename),
    size: `${row.chunk_count} chunk${row.chunk_count === 1 ? '' : 's'}`,
    chunks: row.chunk_count,
    category: 'Uploaded Documents',
    updatedAt: formatDate(row.created_at),
    uploaded: true,
  }
}

function sourceScore(source: ChatSource) {
  const score = source.score <= 1 ? source.score * 100 : source.score
  return Math.max(0, Math.min(100, Math.round(score)))
}

function mapSource(source: ChatSource, index: number): RetrievedDocument {
  return {
    id: source.filename,
    name: source.filename,
    section: `Retrieved source ${index + 1}`,
    score: sourceScore(source),
    category: 'Uploaded Documents',
  }
}

function apiErrorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError) {
    if (error.status === 401) return 'Your session expired. Please sign in again.'
    if (error.status === 429) return 'Request limit reached. Please wait and try again.'
    return error.message || fallback
  }

  return fallback
}

export function AppProvider({ children, userEmail, onLogout }: AppProviderProps) {
  const [conversations, setConversations] = useState<Conversation[]>(() => readConversations(userEmail))
  const [activeConversationId, setActiveConversationId] = useState(createConversationId)
  const [loadingConversationId, setLoadingConversationId] = useState<string | null>(null)
  const [documents, setDocuments] = useState<PolicyDocument[]>([])
  const [selectedCategory, setCategory] = useState('All Documents')
  const [retrievedDocuments, setRetrievedDocuments] = useState<RetrievedDocument[]>([])
  const [suggestions, setSuggestions] = useState(defaultSuggestions)
  const [confidence, setConfidence] = useState(0)
  const [metadata, setMetadata] = useState<ResponseMetadata | null>(null)
  const [theme, setTheme] = useState<Theme>(readTheme)
  const [notifications, setNotifications] = useState<NotificationItem[]>([])
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [bookmarks, setBookmarks] = useState<ChatItem[]>([])
  const [recentQuestions, setRecentQuestions] = useState<string[]>([])
  const [view, setView] = useState<View>('chat')
  const [toast, setToast] = useState('')
  const [selectedDocument, setSelectedDocument] = useState<PolicyDocument | null>(null)
  const activeConversationIdRef = useRef(activeConversationId)

  const messages = useMemo(() => conversations.find(conversation => conversation.id === activeConversationId)?.messages ?? [], [activeConversationId, conversations])
  const loading = loadingConversationId === activeConversationId

  const user = useMemo<User>(() => ({
    id: userEmail,
    name: userEmail,
    role: 'Authenticated user',
    initials: initialsFromEmail(userEmail),
  }), [userEmail])

  useEffect(() => {
    activeConversationIdRef.current = activeConversationId
  }, [activeConversationId])

  const showToast = useCallback((message: string) => {
    setToast(message)
    window.setTimeout(() => setToast(''), 3000)
  }, [])

  const logout = useCallback(() => {
    setDocuments([])
    setRetrievedDocuments([])
    setBookmarks([])
    setRecentQuestions([])
    onLogout()
  }, [onLogout])

  const refreshDocuments = useCallback(async () => {
    try {
      const result = await listDocuments()
      setDocuments(result.documents.map(mapDocument))
    } catch (error) {
      showToast(apiErrorMessage(error, 'Unable to load documents.'))
      if (error instanceof ApiError && error.status === 401) logout()
    }
  }, [logout, showToast])

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    localStorage.setItem('rag-theme', JSON.stringify(theme))
  }, [theme])

  useEffect(() => {
    try {
      localStorage.setItem(conversationStorageKey(userEmail), JSON.stringify(conversations))
    } catch {
      // Keep the current session usable when browser storage is unavailable.
    }
  }, [conversations, userEmail])

  useEffect(() => {
    void refreshDocuments()
  }, [refreshDocuments])

  const setSelectedCategory = useCallback((category: string) => {
    setCategory(category)
    setSuggestions(defaultSuggestions)
  }, [])

  const newChat = useCallback(() => {
    setActiveConversationId(createConversationId())
    setRetrievedDocuments([])
    setConfidence(0)
    setMetadata(null)
    setView('chat')
    setSidebarOpen(false)
    showToast('New conversation started')
  }, [showToast])

  const selectConversation = useCallback((id: string) => {
    if (!conversations.some(conversation => conversation.id === id)) return
    setActiveConversationId(id)
    setRetrievedDocuments([])
    setConfidence(0)
    setMetadata(null)
    setView('chat')
    setSidebarOpen(false)
  }, [conversations])

  const renameConversation = useCallback((id: string, title: string) => {
    const normalized = title.replace(/\s+/g, ' ').trim()
    if (!normalized) return
    setConversations(previous => previous.map(conversation => conversation.id === id ? { ...conversation, title: normalized.slice(0, 48) } : conversation))
  }, [])

  const deleteConversation = useCallback((id: string) => {
    setConversations(previous => previous.filter(conversation => conversation.id !== id))
    if (activeConversationIdRef.current === id) {
      setActiveConversationId(createConversationId())
      setRetrievedDocuments([])
      setConfidence(0)
      setMetadata(null)
      setView('chat')
    }
    showToast('Conversation deleted')
  }, [showToast])

  const toggleConversationPin = useCallback((id: string) => {
    setConversations(previous => previous.map(conversation => {
      if (conversation.id !== id) return conversation
      const isPinned = !conversation.isPinned
      return { ...conversation, isPinned, pinnedAt: isPinned ? new Date().toISOString() : null }
    }))
  }, [])

  const sendMessage = useCallback(async (question: string) => {
    const trimmed = question.trim()
    if (!trimmed || loadingConversationId) return

    const started = performance.now()
    const conversationId = activeConversationId
    const now = new Date().toISOString()
    const userMessage: ChatItem = { id: Date.now(), role: 'user', content: trimmed }
    setConversations(previous => {
      const existing = previous.find(conversation => conversation.id === conversationId)
      if (existing) return previous.map(conversation => conversation.id === conversationId ? { ...conversation, messages: [...conversation.messages, userMessage], updatedAt: now } : conversation)
      return [{ id: conversationId, title: createConversationTitle(trimmed), createdAt: now, updatedAt: now, messages: [userMessage], isPinned: false, pinnedAt: null }, ...previous]
    })
    setRecentQuestions(previous => [trimmed, ...previous.filter(item => item !== trimmed)].slice(0, 8))
    setLoadingConversationId(conversationId)
    setView('chat')

    try {
      const response = await sendChatMessage(trimmed)
      const sources = response.sources.map(mapSource)
      const averageScore = sources.length
        ? Math.round(sources.reduce((total, source) => total + source.score, 0) / sources.length)
        : 0

      const assistantMessage: ChatItem = { id: Date.now() + 1, role: 'assistant', content: response.answer, source: sources[0] }
      setConversations(previous => previous.map(conversation => conversation.id === conversationId ? { ...conversation, messages: [...conversation.messages, assistantMessage], updatedAt: new Date().toISOString() } : conversation))
      if (activeConversationIdRef.current === conversationId) {
        setRetrievedDocuments(sources)
        setConfidence(averageScore)
        setMetadata({
          embeddingModel: 'Backend embedding service',
          llmModel: 'Configured Groq model',
          chunksRetrieved: sources.length,
          latency: `${((performance.now() - started) / 1000).toFixed(2)} sec`,
          timestamp: new Date().toLocaleString(),
        })
      }
    } catch (error) {
      const message = apiErrorMessage(error, 'Unable to answer right now. Please try again.')
      const errorMessage: ChatItem = { id: Date.now() + 1, role: 'assistant', content: message }
      setConversations(previous => previous.map(conversation => conversation.id === conversationId ? { ...conversation, messages: [...conversation.messages, errorMessage], updatedAt: new Date().toISOString() } : conversation))
      showToast(message)
      if (error instanceof ApiError && error.status === 401) logout()
    } finally {
      setLoadingConversationId(current => current === conversationId ? null : current)
    }
  }, [activeConversationId, loadingConversationId, logout, showToast])

  const clearChat = useCallback(() => {
    const currentId = activeConversationIdRef.current
    setConversations(previous => previous.filter(conversation => conversation.id !== currentId))
    setActiveConversationId(createConversationId())
    setRetrievedDocuments([])
    setConfidence(0)
    setMetadata(null)
    setSuggestions(defaultSuggestions)
    showToast('Conversation cleared')
  }, [showToast])

  const uploadDocuments = useCallback(async (files: File[]) => {
    const results: UploadResponse[] = []

    for (const file of files) {
      results.push(await uploadDocument(file))
    }

    await refreshDocuments()
    setNotifications(previous => [{
      id: `n-${Date.now()}`,
      title: 'Document uploaded',
      description: `${results.length} document${results.length === 1 ? '' : 's'} indexed successfully`,
      time: 'Just now',
      read: false,
      tone: 'green',
    }, ...previous])
    showToast(`${results.length} document${results.length === 1 ? '' : 's'} uploaded successfully`)

    return results
  }, [refreshDocuments, showToast])

  const removeDocument = useCallback(async (id: string) => {
    try {
      const result = await deleteDocument(id)
      setDocuments(previous => previous.filter(document => document.id !== id))
      setRetrievedDocuments(previous => previous.filter(document => document.id !== id))
      showToast(result.file_note)
    } catch (error) {
      const message = apiErrorMessage(error, 'Unable to delete document.')
      showToast(message)
      if (error instanceof ApiError && error.status === 401) logout()
      throw error
    }
  }, [logout, showToast])

  const toggleTheme = useCallback(() => {
    setTheme(previous => {
      const next = previous === 'light' ? 'dark' : 'light'
      showToast(`${next === 'dark' ? 'Dark' : 'Light'} theme enabled`)
      return next
    })
  }, [showToast])

  const markNotificationsRead = useCallback(() => {
    setNotifications(previous => previous.map(notification => ({ ...notification, read: true })))
  }, [])

  const updateMessage = useCallback((id: number, patch: Partial<ChatItem>) => {
    const conversationId = activeConversationIdRef.current
    setConversations(previous => previous.map(conversation => conversation.id === conversationId ? { ...conversation, messages: conversation.messages.map(message => message.id === id ? { ...message, ...patch } : message), updatedAt: new Date().toISOString() } : conversation))
    if ('bookmarked' in patch) {
      setBookmarks(previous => {
        const current = messages.find(message => message.id === id)
        if (!current || !patch.bookmarked) return previous.filter(message => message.id !== id)
        return [{ ...current, ...patch }, ...previous.filter(message => message.id !== id)]
      })
    }
  }, [messages])

  const regenerate = useCallback(() => {
    showToast('Ask the question again to generate a fresh answer.')
  }, [showToast])

  const clearHistory = useCallback(() => {
    setRecentQuestions([])
    showToast('Recent question history cleared')
  }, [showToast])

  const value = useMemo(() => ({
    user,
    messages,
    conversations,
    activeConversationId,
    documents,
    selectedCategory,
    retrievedDocuments,
    suggestions,
    confidence,
    metadata,
    theme,
    notifications,
    sidebarOpen,
    loading,
    bookmarks,
    recentQuestions,
    view,
    toast,
    selectedDocument,
    setSelectedCategory,
    setSidebarOpen,
    setView,
    setSelectedDocument,
    showToast,
    newChat,
    selectConversation,
    renameConversation,
    deleteConversation,
    toggleConversationPin,
    sendMessage,
    clearChat,
    uploadDocuments,
    removeDocument,
    toggleTheme,
    markNotificationsRead,
    updateMessage,
    regenerate,
    clearHistory,
    logout,
  }), [activeConversationId, bookmarks, clearChat, clearHistory, confidence, conversations, deleteConversation, documents, loading, logout, markNotificationsRead, messages, metadata, newChat, notifications, recentQuestions, regenerate, removeDocument, renameConversation, retrievedDocuments, selectedCategory, selectedDocument, selectConversation, sendMessage, showToast, sidebarOpen, suggestions, theme, toggleConversationPin, toggleTheme, updateMessage, uploadDocuments, user, view, toast])

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>
}

export function useApp() {
  const context = useContext(AppContext)
  if (!context) throw new Error('useApp must be used inside AppProvider')
  return context
}
