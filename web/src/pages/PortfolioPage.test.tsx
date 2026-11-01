import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { PortfolioPage, PortfolioData } from "./PortfolioPage";

const mock = vi.hoisted(() => ({
  setActiveTenant: vi.fn(),
  state: {
    data: null as PortfolioData | null,
    error: null as string | null,
    loading: false,
  },
}));
vi.mock("../context/AppContext", () => ({
  useApp: () => ({
    currency: "USD",
    startDate: "2026-10-01T00:00:00Z",
    endDate: "2026-10-03T00:00:00Z",
    token: "test",
    tenants: [{ id: "a", name: "Store A" }],
    setActiveTenant: mock.setActiveTenant,
    triggerRefresh: vi.fn(),
  }),
}));
vi.mock("../lib/usePolling", () => ({ usePolling: () => mock.state }));
vi.mock("../components/Charts", () => ({
  TrendLineChart: () => <div>Combined chart</div>,
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  mock.state.error = null;
});

function data(): PortfolioData {
  return {
    complete: true,
    tenant_count: 1,
    ready_tenant_count: 1,
    retrieval_ms: 123,
    read_completed_at: "2026-10-03T00:00:00Z",
    totals: { total_order_count: 3, sales_amount: "0.30" },
    buckets: [],
    tenants: [
      {
        tenant_id: "a",
        name: "Store A",
        status: "ready",
        metrics: { total_order_count: 3, sales_amount: "0.30" },
        freshness: null,
        error: null,
      },
    ],
  };
}

describe("authorized portfolio overview", () => {
  it("shows totals and supports tenant search and drill-down", () => {
    mock.state.data = data();
    const open = vi.fn();
    render(<PortfolioPage onDrillDown={open} />);
    expect(screen.getByText("All Tenants Overview")).toBeTruthy();
    expect(screen.getAllByText("$0.30")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Open Store A" }));
    expect(mock.setActiveTenant).toHaveBeenCalledWith({
      id: "a",
      name: "Store A",
    });
    expect(open).toHaveBeenCalledOnce();
    fireEvent.change(screen.getByRole("textbox", { name: "Search tenants" }), {
      target: { value: "missing" },
    });
    expect(screen.getByText("No matching authorized stores.")).toBeTruthy();
  });

  it("shows partial coverage without combined totals or chart", () => {
    mock.state.data = {
      ...data(),
      complete: false,
      ready_tenant_count: 0,
      totals: null,
      buckets: null,
    };
    render(<PortfolioPage onDrillDown={vi.fn()} />);
    expect(screen.getByRole("alert").textContent).toContain("withheld");
    expect(screen.queryByText("Combined chart")).toBeNull();
    expect(screen.getAllByText("Unavailable")).toHaveLength(2);
  });

  it("never presents a previous response after refresh failure", () => {
    mock.state.data = data();
    mock.state.error = "401: expired";
    render(<PortfolioPage onDrillDown={vi.fn()} />);
    expect(screen.getByRole("alert").textContent).toContain("401");
    expect(screen.queryByText("Store A")).toBeNull();
    expect(screen.queryByText("Combined chart")).toBeNull();
  });
});
