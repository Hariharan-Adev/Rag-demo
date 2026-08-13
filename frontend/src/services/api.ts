const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'bmp', 'gif', 'tiff', 'webp'])
const INGESTION_WAIT_TIMEOUT_MS = 5 * 60 * 1000

let accessToken = ''

export class ApiError extends Error {
  status: number
  code?: string
  retryable?: boolean

  constructor(message: string, status: number, code?: string, retryable?: boolean) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.retryable = retryable
  }
}

export interface LoginResponse {
  access_token: string
  token_type: string
}

export interface ForgotPasswordResponse {
  message: string
}

export interface UploadResponse {
  message: string
  document_id: number
  version_id?: number
  job_id?: string | null
  filename?: string
  chunk_count?: number
  status: 'uploaded' | 'duplicate_content_reused' | 'processed' | 'accepted' | 'queued' | 'processing' | 'retry_scheduled' | 'completed' | 'failed' | 'cancelled'
  display_filename?: string
  relative_path?: string | null
  duplicate_type?: string | null
  content_reused?: boolean
  file_type?: string
  document_type?: 'screenshot' | 'image'
  extraction?: 'ocr+vision-with-ocr-fallback'
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
  visibility?: 'private' | 'organization'
  status?: string
  current_version_id?: number | null
  current_version_number?: number | null
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
  archive_extensions: string[]
  max_file_size_mb: number
  max_zip_upload_mb: number
  max_folder_files: number
  max_folder_total_size_mb: number
  max_concurrent_uploads: number
}

export interface ZipUploadFileResult {
  filename: string
  status: 'uploaded' | 'duplicate' | 'duplicate_content_reused' | 'rejected' | 'failed'
  document_id: number | null
  display_filename?: string
  message?: string
  reason?: string
}

export interface ZipUploadResponse {
  archive: string
  status: 'completed' | 'partially_completed'
  summary: {
    total_entries: number
    uploaded: number
    duplicates: number
    failed: number
  }
  files: ZipUploadFileResult[]
}

export interface ListDocumentsResponse {
  documents: DocumentRecord[]
}

export interface ChatSource {
  document_id?: number
  version_id?: number
  filename: string
  text?: string
  source_type: string
  source_location: Record<string, string | number | boolean | null | Array<Record<string, number>>>
  location?: Record<string, string | number | boolean | null | Array<Record<string, number>>>
  retrieval_score: number | null
}

export interface ChatResponse {
  answer: string
  grounded: boolean
  sources: ChatSource[]
}

export interface ChatHistoryMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations: ChatSource[]
  created_at: string
}

export interface ChatHistoryConversation {
  id: string
  title: string
  created_at: string
  updated_at: string
  is_pinned: boolean
  pinned_at?: string | null
  messages: ChatHistoryMessage[]
}

export interface ListChatHistoryResponse {
  conversations: ChatHistoryConversation[]
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
    const body = await response.json() as {
      detail?: string | { code?: string; message?: string; retryable?: boolean }
    }
    if (typeof body.detail === 'string') return { message: body.detail }
    if (body.detail && typeof body.detail.message === 'string') {
      return {
        message: body.detail.message,
        code: body.detail.code,
        retryable: body.detail.retryable,
      }
    }
    return { message: fallback }
  } catch {
    return { message: fallback }
  }
}

async function requestJson<T>(path: string, options: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, options)

  if (!response.ok) {
    const error = await readError(response, 'Request failed.')
    throw new ApiError(error.message, response.status, error.code, error.retryable)
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

export async function requestPasswordReset(email: string) {
  return requestJson<ForgotPasswordResponse>('/auth/forgot-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  })
}

export async function resetPassword(token: string, newPassword: string) {
  return requestJson<{ message: string }>('/auth/reset-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, new_password: newPassword }),
  })
}

type UploadOptions = {
  collectionId?: number
  batchId?: number
  relativePath?: string
  signal?: AbortSignal
  idempotencyKey?: string
}

function uploadRequestKey(scope: string) {
  const nonce = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`
  return `${scope}:${nonce}`
}

function isImageUpload(file: File) {
  const extension = file.name.split('.').pop()?.toLowerCase() ?? ''
  return file.type.startsWith('image/') || IMAGE_EXTENSIONS.has(extension)
}

export interface IngestionJob {
  job_id: string
  status: 'queued' | 'processing' | 'retry_scheduled' | 'completed' | 'failed' | 'cancelled'
  document_id: number
  version_id: number
  attempt_count: number
  max_attempts: number
  next_retry_at?: string | null
  pipeline_version?: string
  error: { code: string; message: string; retryable?: boolean } | null
  result?: {
    content_reused?: boolean
    reused_deleted_content?: boolean
    message?: string
  } | null
}

export interface DocumentVersion {
  id: number
  version_number: number
  status: 'queued' | 'processing' | 'completed' | 'failed' | 'cancelled'
  ingestion_status: string
  extraction_status: string
  indexing_status: string
  storage_key: string | null
  mime_type: string | null
  file_size: number | null
  source_metadata: Record<string, unknown>
  created_at: string
  completed_at: string | null
  is_current: boolean
  error: { code: string; message: string } | null
}

export async function uploadImage(file: File, options: UploadOptions = {}) {
  const formData = new FormData()
  formData.append('image', file)
  formData.append('document_type', 'screenshot')
  if (options.collectionId !== undefined) formData.append('collection_id', String(options.collectionId))
  if (options.batchId !== undefined) formData.append('upload_batch_id', String(options.batchId))
  if (options.relativePath) formData.append('relative_path', options.relativePath)
  if (options.batchId !== undefined) formData.append('upload_batch_id', String(options.batchId))
  if (options.relativePath) formData.append('relative_path', options.relativePath)

  return requestJson<UploadResponse>('/api/upload-image', {
    method: 'POST',
    headers: authHeaders(),
    body: formData,
    signal: options.signal,
  })
}

export async function uploadDocument(file: File, options: UploadOptions = {}) {
  const formData = new FormData()
  formData.append('file', file)
  if (options.collectionId !== undefined) formData.append('collection_id', String(options.collectionId))
  if (options.batchId !== undefined) formData.append('upload_batch_id', String(options.batchId))
  if (options.relativePath) formData.append('relative_path', options.relativePath)
  const accepted = await requestJson<UploadResponse>('/api/documents/upload', {
    method: 'POST',
    headers: {
      ...authHeaders(),
      // A key identifies one upload action, not the file forever. Re-selecting a
      // previously deleted file must create a fresh document instead of replaying
      // the old completed job.
      'Idempotency-Key': options.idempotencyKey ?? uploadRequestKey('document'),
    },
    body: formData,
    signal: options.signal,
  })
  if (!accepted.job_id || accepted.status === 'completed') return accepted
  if (accepted.status === 'failed') await retryIngestionJob(accepted.job_id)
  const completed = await waitForIngestionJob(accepted.job_id, options.signal)
  if (completed.status === 'failed') {
    throw new ApiError(
      completed.error?.message ?? 'Document processing failed.',
      422,
      completed.error?.code,
      completed.error?.retryable,
    )
  }
  if (completed.status === 'cancelled') {
    throw new ApiError('Document processing was cancelled.', 409)
  }
  return {
    ...accepted,
    status: 'completed' as const,
    message: completed.result?.message ?? 'Document processed successfully.',
    content_reused: completed.result?.content_reused ?? false,
  }
}

export async function uploadDocumentVersion(documentId: string, file: File, signal?: AbortSignal) {
  const formData = new FormData()
  formData.append('file', file)
  const accepted = await requestJson<UploadResponse>(`/api/documents/${documentId}/versions`, {
    method: 'POST',
    headers: {
      ...authHeaders(),
      'Idempotency-Key': uploadRequestKey(`version:${documentId}`),
    },
    body: formData,
    signal,
  })
  if (!accepted.job_id) return accepted
  if (accepted.status === 'failed') await retryIngestionJob(accepted.job_id)
  const completed = await waitForIngestionJob(accepted.job_id, signal)
  if (completed.status !== 'completed') {
    throw new ApiError(completed.error?.message ?? `Version ${completed.status}.`, 422)
  }
  return { ...accepted, status: 'completed' as const }
}

export async function getIngestionJob(jobId: string) {
  return requestJson<IngestionJob>(`/api/jobs/${jobId}`, {
    method: 'GET',
    headers: authHeaders(),
  })
}

export async function waitForIngestionJob(jobId: string, signal?: AbortSignal) {
  const startedAt = Date.now()
  while (true) {
    if (signal?.aborted) throw new DOMException('Upload cancelled.', 'AbortError')
    if (Date.now() - startedAt >= INGESTION_WAIT_TIMEOUT_MS) {
      throw new ApiError(
        'Document processing is taking longer than expected. It may continue in the background; check the library before retrying.',
        504,
      )
    }
    const job = await getIngestionJob(jobId)
    if (['completed', 'failed', 'cancelled'].includes(job.status)) return job
    await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(resolve, 1000)
      signal?.addEventListener('abort', () => {
        window.clearTimeout(timer)
        reject(new DOMException('Upload cancelled.', 'AbortError'))
      }, { once: true })
    })
  }
}

export async function retryIngestionJob(jobId: string) {
  return requestJson<{ job_id: string; status: 'queued' }>(`/api/jobs/${jobId}/retry`, {
    method: 'POST', headers: authHeaders(),
  })
}

export async function cancelIngestionJob(jobId: string) {
  return requestJson<{ job_id: string; status: 'cancelled' }>(`/api/jobs/${jobId}/cancel`, {
    method: 'POST', headers: authHeaders(),
  })
}

export async function uploadZipArchive(
  file: File,
  options: { collectionId?: number; signal?: AbortSignal; idempotencyKey?: string } = {},
) {
  const formData = new FormData()
  formData.append('archive', file)
  if (options.collectionId !== undefined) formData.append('collection_id', String(options.collectionId))

  const accepted = await requestJson<UploadResponse>('/api/documents/upload-zip', {
    method: 'POST',
    headers: {
      ...authHeaders(),
      'Idempotency-Key': options.idempotencyKey ?? uploadRequestKey('archive'),
    },
    body: formData,
    signal: options.signal,
  })
  if (!accepted.job_id) throw new ApiError('Archive job was not created.', 500)
  const parent = await waitForIngestionJob(accepted.job_id, options.signal)
  if (parent.status === 'failed') throw new ApiError(parent.error?.message ?? 'Archive processing failed.', 422)
  const result = parent.result as {
    archive: string
    files: Array<{ filename: string; status: string; document_id?: number; job_id?: string; reason?: string }>
  }
  const files: ZipUploadFileResult[] = []
  for (const entry of result.files) {
    if (entry.status !== 'queued' || !entry.job_id) {
      files.push({
        filename: entry.filename,
        status: entry.status === 'duplicate' ? 'duplicate' : 'rejected',
        document_id: entry.document_id ?? null,
        reason: entry.reason,
      })
      continue
    }
    const child = await waitForIngestionJob(entry.job_id, options.signal)
    files.push({
      filename: entry.filename,
      status: child.status === 'completed' ? 'uploaded' : 'failed',
      document_id: child.document_id,
      reason: child.error?.message,
    })
  }
  const uploaded = files.filter(entry => entry.status === 'uploaded').length
  const duplicates = files.filter(entry => entry.status === 'duplicate' || entry.status === 'duplicate_content_reused').length
  const failed = files.length - uploaded - duplicates
  return {
    archive: result.archive,
    status: failed ? 'partially_completed' : 'completed',
    summary: { total_entries: files.length, uploaded, duplicates, failed },
    files,
  } satisfies ZipUploadResponse
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

export async function sendChatMessage(question: string, collectionId?: number | null, documentId?: number | null, conversationId?: string | null) {
  return requestJson<ChatResponse>('/chat', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(),
    },
    body: JSON.stringify({
      question,
      conversation_id: conversationId ?? null,
      collection_id: collectionId ?? null,
      document_id: documentId ?? null,
    }),
  })
}

export async function listChatConversations() {
  return requestJson<ListChatHistoryResponse>('/chat/conversations', {
    method: 'GET',
    headers: authHeaders(),
  })
}

export async function createChatConversation(id: string, title: string) {
  return requestJson<{ id: string }>('/chat/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ id, title }),
  })
}

export async function updateChatConversation(id: string, patch: { title?: string; is_pinned?: boolean }) {
  return requestJson<{ id: string; updated: boolean }>(`/chat/conversations/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(patch),
  })
}

export async function deleteChatConversation(id: string) {
  return requestJson<{ id: string; deleted: boolean }>(`/chat/conversations/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  })
}

export async function restoreDocument(documentId: string) {
  return requestJson<{ document_id: number; restored: boolean }>(`/documents/${documentId}/restore`, {
    method: 'POST',
    headers: authHeaders(),
  })
}

export async function listDeletedDocuments() {
  return requestJson<{ documents: Array<{
    id: number
    display_filename: string
    deleted_at: string
    current_version_id: number | null
  }> }>('/documents/trash', { method: 'GET', headers: authHeaders() })
}

export async function listDocumentVersions(documentId: string) {
  return requestJson<{ versions: DocumentVersion[] }>(`/documents/${documentId}/versions`, {
    method: 'GET', headers: authHeaders(),
  })
}

export async function makeDocumentVersionCurrent(documentId: string, versionId: number) {
  return requestJson<{ document_id: number; current_version_id: number }>(
    `/documents/${documentId}/versions/${versionId}/make-current`,
    { method: 'POST', headers: authHeaders() },
  )
}
