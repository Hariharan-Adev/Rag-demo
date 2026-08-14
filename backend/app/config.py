"""Application settings loaded from .env."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Keep secrets outside source code."""

    groq_api_key: str = ""
    groq_model: str = ""
    groq_vision_model: str = "qwen/qwen3.6-27b"
    llm_provider: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-12-01-preview"
    azure_openai_rag_deployment: str = ""
    azure_openai_utility_deployment: str = ""
    enable_image_vision: bool = True
    jwt_secret_key: str = ""
    access_token_expire_minutes: int = 30
    rate_limit_salt: str = ""
    chat_requests_per_hour: int = 20
    search_requests_per_hour: int = 60
    uploads_per_hour: int = 25
    password_reset_requests_per_hour: int = 5
    password_reset_token_minutes: int = Field(default=30, ge=5, le=120)
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = "Docsense AI"
    smtp_use_tls: bool = True
    smtp_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    frontend_base_url: str = "http://localhost:5173"
    groq_calls_per_day: int = 50
    groq_daily_token_budget: int = 200000
    groq_daily_cost_cap_usd: float = 5.0
    groq_prompt_cost_per_million: float = 0.0
    groq_completion_cost_per_million: float = 0.0
    max_folder_files: int = 25
    max_file_size_mb: int = 25
    max_folder_total_size_mb: int = 200
    max_concurrent_file_processing: int = 3
    max_zip_upload_mb: int = 50
    max_zip_extracted_mb: int = 250
    max_zip_files: int = 100
    max_zip_compression_ratio: float = 100.0
    max_office_archive_entries: int = 10000
    max_office_uncompressed_mb: int = 250
    max_office_compression_ratio: float = 100.0
    max_pdf_pages: int = 1000
    max_powerpoint_slides: int = 1000
    max_workbook_sheets: int = 250
    max_workbook_rows: int = 200000
    parser_timeout_seconds: float = 120.0
    tesseract_cmd: str = ""
    ocr_required_languages: str = "eng"
    include_hidden_worksheets: bool = True
    include_very_hidden_worksheets: bool = False
    default_organization_name: str = "Default Organization"
    ingestion_max_attempts: int = 5
    ingestion_lock_seconds: int = 300
    ingestion_poll_seconds: float = 1.0
    ingestion_backoff_base_seconds: float = 2.0
    ingestion_backoff_max_seconds: float = 3600.0
    ingestion_pipeline_version: str = "v1"
    vector_store: str = ""
    vector_store_provider: str = "qdrant"
    vector_store_rollback_dual_write: bool = False
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_collection: str = "rag_chunks"
    qdrant_mode: str = "auto"
    qdrant_prefer_grpc: bool = False
    qdrant_path: str = ""
    qdrant_local_path: str = "data/qdrant"
    embedding_provider: str = "local"
    local_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dimension: int = 384
    embedding_batch_size: int = 64
    embedding_model_version: str = "all-MiniLM-L6-v2"
    embedding_model_load_timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)
    chat_follow_up_context_minutes: int = Field(default=30, ge=1, le=360)
    rag_retrieval_limit: int = Field(default=15, ge=1, le=100)
    rag_final_context_limit: int = Field(default=5, ge=1, le=20)
    rag_final_context_token_budget: int = Field(default=6000, ge=256, le=32000)
    rag_complementary_context_limit: int = Field(default=3, ge=1, le=10)
    rag_complementary_min_score: float = Field(default=0.15, ge=0.0, le=1.0)
    rag_neighbor_expansion_min_score: float = Field(default=0.50, ge=0.0)
    rag_neighbor_expansion_max_neighbors: int = Field(default=2, ge=0, le=4)
    # A weak-evidence floor for vector candidates; source evidence still decides grounding.
    rag_min_score: float = Field(default=0.30, ge=-1.0, le=1.0)
    rag_retrieval_mode: str = "hybrid"
    rag_vector_candidate_limit: int = Field(default=30, ge=1, le=200)
    rag_keyword_candidate_limit: int = Field(default=30, ge=1, le=200)
    rag_rrf_k: int = Field(default=60, ge=1, le=1000)
    rag_structured_result_limit: int = Field(default=100, ge=1, le=1000)
    rag_diagnostics_enabled: bool = False
    embedded_ocr_max_images_per_document: int = Field(default=25, ge=0, le=500)
    embedded_ocr_max_images_per_page: int = Field(default=5, ge=0, le=100)
    embedded_ocr_max_pixels: int = Field(default=16_000_000, ge=1, le=100_000_000)
    embedded_ocr_max_decoded_bytes: int = Field(default=20_000_000, ge=1, le=200_000_000)
    opensearch_url: str = ""
    opensearch_username: str = ""
    opensearch_password: str = ""
    opensearch_index: str = "rag_chunks"
    hard_delete_enabled: bool = False
    app_environment: str = "development"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
