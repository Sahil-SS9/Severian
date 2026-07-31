import { useEffect, useId, useState, type ReactNode } from "react";
import { Archive, Database, Gear, HardDrives, House, MagnifyingGlass, Queue, Warning } from "@phosphor-icons/react";
import { BrowserRouter, NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api } from "./api/client";
import type { Page } from "./api/contracts";

type ResourceName = "sources" | "memories" | "jobs" | "backups";
const nav = [
  ["Overview", "/", House], ["Sources", "/sources", Database], ["Memories", "/memories", Archive],
  ["Search", "/search", MagnifyingGlass], ["Jobs", "/jobs", Queue], ["Backups", "/backups", HardDrives], ["Settings", "/settings", Gear],
] as const;
const descriptions: Record<string, string> = {
  Overview: "System status and activity across your local memory service.", Sources: "Original evidence retained by Severian.",
  Memories: "Derived knowledge with provenance and revision history.", Search: "Search indexed memories and preview cited context.",
  Jobs: "Background ingestion, indexing, and maintenance work.", Backups: "Portable snapshots for recovery and export.",
  Settings: "Service policy, storage, and administrative controls.",
};

function Shell({ children }: { children: ReactNode }) {
  return <div className="app-shell"><a className="skip-link" href="#main">Skip to content</a><aside className="sidebar">
    <div className="brand"><span className="brand-mark" aria-hidden="true">S</span><div><strong>Severian</strong><small>Memory admin</small></div></div>
    <nav aria-label="Primary navigation">{nav.map(([label, to, Icon]) => <NavLink key={to} to={to} end={to === "/"} className={({isActive}) => isActive ? "nav-link active" : "nav-link"}><Icon size={19} weight="regular" aria-hidden="true"/><span>{label}</span></NavLink>)}</nav>
    <div className="scope"><span>Active scope</span><strong>Local workspace</strong></div>
  </aside><main id="main" tabIndex={-1}>{children}</main></div>;
}

function Header({ title, action }: { title: string; action?: ReactNode }) {
  return <header className="page-header"><div><h1>{title}</h1><p>{descriptions[title]}</p></div>{action}</header>;
}

function StatePanel({ kind, title, children, onRetry }: { kind: "loading" | "empty" | "error"; title: string; children?: ReactNode; onRetry?: () => void }) {
  return <section className={`state-panel ${kind}`} role={kind === "error" ? "alert" : kind === "loading" ? "status" : undefined} aria-live="polite">
    {kind === "error" && <Warning size={28} aria-hidden="true"/>}<h2>{title}</h2>{children && <p>{children}</p>}{onRetry && <button className="button secondary" onClick={onRetry}>Retry</button>}
  </section>;
}

function ResourcePage({ name }: { name: ResourceName }) {
  const title = name[0].toUpperCase() + name.slice(1); const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{loading: boolean; data?: Page<unknown>; error?: string}>({loading: true});
  useEffect(() => { const controller = new AbortController(); setState({loading: true});
    api[name](controller.signal).then(data => setState({loading: false, data})).catch(error => { if (error instanceof DOMException && error.name === "AbortError") return; setState({loading: false, error: error instanceof Error ? error.message : "Unknown error"}); });
    return () => controller.abort();
  }, [name, attempt]);
  return <div className="page"><Header title={title}/>{state.loading ? <StatePanel kind="loading" title={`Loading ${name}`}><span className="skeleton-lines" aria-hidden="true"/></StatePanel> : state.error ? <StatePanel kind="error" title={`Could not load ${name}`} onRetry={() => setAttempt(v => v + 1)}>{state.error}</StatePanel> : state.data?.total === 0 ? <StatePanel kind="empty" title={`No ${name} yet`}>{emptyCopy[name]}</StatePanel> : <section className="data-ready"><h2>{state.data?.total} {name}</h2><p>The API returned records. Detailed tables connect in the next implementation slice.</p></section>}</div>;
}
const emptyCopy: Record<ResourceName, string> = { sources: "Connect or import a source to preserve original evidence.", memories: "Memories will appear after a source has been ingested and processed.", jobs: "Background work will appear here when ingestion or maintenance starts.", backups: "Create a backup when the canonical store is available." };

function Overview() { return <div className="page"><Header title="Overview"/><section className="summary-strip" aria-label="Workspace summary"><div><span>Canonical store</span><strong>Not connected</strong></div><div><span>Search index</span><strong>Awaiting service</strong></div><div><span>Scope</span><strong>Local workspace</strong></div></section><StatePanel kind="empty" title="Waiting for the Severian API">This interface is ready to connect to /v1. No service data has been assumed.</StatePanel></div>; }
function SearchPage() { return <div className="page"><Header title="Search"/><form className="search-form" onSubmit={e => e.preventDefault()}><label htmlFor="query">Search memories</label><div><input id="query" name="query" type="search" placeholder="Enter a person, project, or decision"/><button className="button primary" type="submit">Search</button></div><small>Results will include source citations and scope information.</small></form><StatePanel kind="empty" title="Start with a search">Enter a query to search indexed memories. No results are shown until the API responds.</StatePanel></div>; }
function ConfirmDialog({ onClose }: { onClose: () => void }) { const [value, setValue] = useState(""); const label = useId(); return <div className="dialog-backdrop"><div role="dialog" aria-modal="true" aria-labelledby={`${label}-title`} className="dialog"><h2 id={`${label}-title`}>Delete all Severian data</h2><p>This will remove canonical sources, memories, indexes, and audit history. This scaffold does not send the operation yet.</p><label htmlFor={label}>Type <strong>DELETE ALL</strong> to confirm</label><input id={label} value={value} onChange={e => setValue(e.target.value)} autoFocus/><div className="dialog-actions"><button className="button secondary" onClick={onClose}>Cancel</button><button className="button danger" disabled={value !== "DELETE ALL"}>Permanently delete</button></div></div></div>; }
function Settings() { const [open, setOpen] = useState(false); return <div className="page"><Header title="Settings"/><section className="settings-section"><h2>Service connection</h2><dl><div><dt>API base</dt><dd>/v1</dd></div><div><dt>Deployment</dt><dd>Local, single tenant</dd></div></dl></section><section className="danger-zone"><div><h2>Danger zone</h2><p>Destructive operations require an explicit phrase before they can be submitted.</p></div><button className="button danger-outline" onClick={() => setOpen(true)}>Delete all data</button></section>{open && <ConfirmDialog onClose={() => setOpen(false)}/>}</div>; }

export function App() { return <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><Shell><Routes><Route path="/" element={<Overview/>}/><Route path="/sources" element={<ResourcePage name="sources"/>}/><Route path="/memories" element={<ResourcePage name="memories"/>}/><Route path="/search" element={<SearchPage/>}/><Route path="/jobs" element={<ResourcePage name="jobs"/>}/><Route path="/backups" element={<ResourcePage name="backups"/>}/><Route path="/settings" element={<Settings/>}/><Route path="*" element={<Navigate to="/" replace/>}/></Routes></Shell></BrowserRouter>; }
