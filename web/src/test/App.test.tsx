import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";

const ok = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
afterEach(() => { vi.restoreAllMocks(); window.history.pushState({}, "", "/"); });

describe("Severian admin shell", () => {
  it("navigates every primary destination with an accessible current-page marker", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(() => ok({ items: [], total: 0 }));
    const user = userEvent.setup(); render(<App />);
    for (const name of ["Sources", "Memories", "Search", "Jobs", "Backups", "Settings", "Overview"]) {
      await user.click(screen.getByRole("link", { name }));
      expect(screen.getByRole("heading", { level: 1, name })).toBeInTheDocument();
      expect(screen.getByRole("link", { name })).toHaveAttribute("aria-current", "page");
    }
  });

  it("shows an honest loading state then a useful empty source state", async () => {
    let resolve!: (response: Response) => void;
    vi.spyOn(globalThis, "fetch").mockReturnValue(new Promise(r => { resolve = r; }));
    window.history.pushState({}, "", "/sources"); render(<App />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading sources");
    resolve(new Response(JSON.stringify({ items: [], total: 0 }), { status: 200, headers: { "Content-Type": "application/json" } }));
    expect(await screen.findByText("No sources yet")).toBeInTheDocument();
    expect(screen.getByText(/Connect or import a source/)).toBeInTheDocument();
  });

  it("renders API errors without pretending data loaded and supports retry", async () => {
    const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response("unavailable", { status: 503 })).mockImplementationOnce(() => ok({ items: [], total: 0 }));
    window.history.pushState({}, "", "/memories"); const user = userEvent.setup(); render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load memories");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No memories yet")).toBeInTheDocument();
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("requires typed confirmation before enabling a destructive action", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(() => ok({ items: [], total: 0 }));
    window.history.pushState({}, "", "/settings"); const user = userEvent.setup(); render(<App />);
    await user.click(await screen.findByRole("button", { name: "Delete all data" }));
    const dialog = screen.getByRole("dialog", { name: "Delete all Severian data" });
    const confirm = screen.getByRole("button", { name: "Permanently delete" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText(/Type DELETE ALL/), "DELETE ALL");
    expect(confirm).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
  });
});
