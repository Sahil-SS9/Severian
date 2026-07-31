export interface Page<T> { items: T[]; total: number }
export interface SourceSummary { id: string; name: string; kind: string; created_at: string }
export interface MemorySummary { id: string; content: string; memory_type: string; confidence: number | null; created_at: string }
export interface JobSummary { id: string; kind: string; status: "queued" | "running" | "succeeded" | "failed"; created_at: string }
export interface BackupSummary { id: string; created_at: string; size_bytes: number; status: "creating" | "ready" | "failed" }
export interface HealthSummary { status: "healthy" | "degraded" | "unavailable"; version?: string }
export interface SearchResult { id: string; excerpt: string; score: number; source_ids: string[] }
