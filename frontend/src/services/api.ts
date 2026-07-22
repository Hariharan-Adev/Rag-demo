const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'

let accessToken = ''

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export interface LoginResponse {
  access_token: string
  token_type: string
}

export interface UploadResponse {
  message: string
  document_id: number
  filename: string
  chunk_count: number
  status: 'uploaded' | 'duplicate_content_reused' | 'processed' | 'accepted'
  display_filename?: string
  relative_path?: string | null
  duplicate_type?: string | null
  content_reused?: boolean
}

export interface DocumentRecord {
  id: number
  filename: string
  created_at: string
  chunk_count: number
  collection_id?: number | null
  collection_name?: string | null
  upload_batch_id?: number | null
  relative_path?: string | null
}

export interface CollectionRecord {
  id: number
  name: string
  document_count?: number
  created_at: string
  updated_at: string
}

export interface UploadBatchRecord {
  id: number
  collection_id: number
  original_folder_name: string
  status: string
  total_files: number
  processed_files: number
  successful_files: number
  duplicate_files: number
  skipped_files: number
  failed_files: number
}

export interface UploadConfig {
  supported_extensions: string[]
  max_file_size_mb: number
  max_folder_files: number
  max_folder_total_size_mb: number
  max_concurrent_uploads: number
}

export interface ListDocumentsResponse {
  documents: DocumentRecord[]
}

export interface ChatSource {
  filename: string
  score: number
}

export interface ChatResponse {
  answer: string
  sources: ChatSource[]
}

export interface DeleteDocumentResponse {
  message: string
  document_id: number
  file_deleted: boolean
  file_note: string
}

export function setAccessToken(token: string) {
  accessToken = token
}

function authHeaders(): HeadersInit {
  return accessToken ? { Authorization: `Bearer ${accessToken}` } : {}
}

async function readError(response: Response, fallback: string) {
  try {
    const body = await response.json() as { detail?: unknown }
    return typeof body.detail === 'string' ? body.detail : fallback
  } catch {
    return fallback
  }
}

async function requestJson<T>(path: string, options: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, options)

  if (!response.ok) {
    throw new ApiError(await readError(response, 'Request failed.'), response.status)
  }

  return response.json() as Promise<T>
}

export async function register(email: string, password: string) {
  await requestJson('/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
}

export async function login(email: string, password: string) {
  const formData = new URLSearchParams({ username: email, password })

  return requestJson<LoginResponse>('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: formData,
  })
}

export async function uploadDocument(file: File, options: { collectionId?: number; batchId?: number; relativePath?: string; signal?: AbortSignal } = {}) {
  const formData = new FormData()
  formData.append('file', file)
  if (options.collectionId !== undefined) formData.append('collection_id', String(options.collectionId))
  if (options.batchId !== undefined) formData.append('upload_batch_id', String(options.batchId))
  if (options.relativePath) formData.append('relative_path', options.relativePath)

  return requestJson<UploadResponse>('/documents/upload', {
    method: 'POST',
    headers: authHeaders(),
    body: formData,
    signal: options.signal,
  })
}

export async function getUploadConfig() {
  return requestJson<UploadConfig>('/documents/upload-config', { method: 'GET', headers: authHeaders() })
}

export async function listCollections() {
  return requestJson<{ collections: CollectionRecord[] }>('/collections', { method: 'GET', headers: authHeaders() })
}

export async function createCollection(name: string) {
  return requestJson<CollectionRecord>('/collections', {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify({ name }),
  })
}

export async function createUploadBatch(collectionId: number, folderName: string, totalFiles: number, totalBytes: number) {
  return requestJson<UploadBatchRecord>('/upload-batches', {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ collection_id: collectionId, original_folder_name: folderName, total_files: totalFiles, total_bytes: totalBytes }),
  })
}

export async function getUploadBatch(batchId: number) {
  return requestJson<UploadBatchRecord>(`/upload-batches/${batchId}`, { method: 'GET', headers: authHeaders() })
}

export async function skipUploadBatchFiles(batchId: number, count: number) {
  return requestJson<UploadBatchRecord>(`/upload-batches/${batchId}/skip`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify({ count }),
  })
}

export async function cancelUploadBatch(batchId: number) {
  return requestJson<{ status: string; batch_id: number }>(`/upload-batches/${batchId}/cancel`, { method: 'POST', headers: authHeaders() })
}

export async function listDocuments() {
  return requestJson<ListDocumentsResponse>('/documents', {
    method: 'GET',
    headers: authHeaders(),
  })
}

export async function deleteDocument(documentId: string) {
  return requestJson<DeleteDocumentResponse>(`/documents/${documentId}`, {
    method: 'DELETE',
    headers: authHeaders(),
  })
}

export async function sendChatMessage(question: string, collectionId?: number | null) {
  return requestJson<ChatResponse>('/chat', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(),
    },
    body: JSON.stringify({ question, collection_id: collectionId ?? null }),
  })
}
