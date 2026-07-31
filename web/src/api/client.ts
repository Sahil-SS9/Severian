import type { BackupSummary, HealthSummary, JobSummary, MemorySummary, Page, SearchResult, SourceSummary } from "./contracts";

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) { super(message); this.name = "ApiError"; }
}

export interface SeverianApi {
  health(signal?: AbortSignal): Promise<HealthSummary>;
  sources(signal?: AbortSignal): Promise<Page<SourceSummary>>;
  memories(signal?: AbortSignal): Promise<Page<MemorySummary>>;
  jobs(signal?: AbortSignal): Promise<Page<JobSummary>>;
  backups(signal?: AbortSignal): Promise<Page<BackupSummary>>;
  search(query: string, signal?: AbortSignal): Promise<Page<SearchResult>>;
}

async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try { response = await fetch(`/v1${path}`, { headers: { Accept: "application/json" }, signal }); }
  catch (error) { if (error instanceof DOMException && error.name === "AbortError") throw error; throw new ApiError(0, "The Severian service could not be reached."); }
  if (!response.ok) throw new ApiError(response.status, `The Severian service returned ${response.status}.`);
  return response.json() as Promise<T>;
}

export const api: SeverianApi = {
  health: signal => request("/health", signal), sources: signal => request("/sources", signal),
  memories: signal => request("/memories", signal), jobs: signal => request("/jobs", signal),
  backups: signal => request("/backups", signal),
  search: (query, signal) => request(`/search?q=${encodeURIComponent(query)}`, signal),
};
