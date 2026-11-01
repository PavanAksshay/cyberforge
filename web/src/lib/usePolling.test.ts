import { act, renderHook, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePolling } from "./usePolling";
const context = vi.hoisted(() => ({ autoRefreshInterval: 5, refreshKey: 0 }));
vi.mock("../context/AppContext", () => ({ useApp: () => context }));
beforeEach(() => {
  vi.useFakeTimers();
  context.autoRefreshInterval = 5;
  context.refreshKey = 0;
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
describe("nonoverlapping authenticated view polling", () => {
  it("requests at 5s, ticks no extra HTTP every second, handles off/manual and cleans up", async () => {
    const load = vi.fn(async () => ({ revision: 7 }));
    const { result, rerender, unmount } = renderHook(() =>
      usePolling("tenant-a:USD", load),
    );
    await act(async () => {});
    expect(load).toHaveBeenCalledTimes(1);
    const fetched = result.current.fetchedAt;
    await act(async () => vi.advanceTimersByTimeAsync(4000));
    expect(load).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(load).toHaveBeenCalledTimes(2);
    expect(result.current.fetchedAt?.getTime()).toBeGreaterThan(
      fetched!.getTime(),
    );
    context.autoRefreshInterval = 0;
    rerender();
    await act(async () => {});
    const off = load.mock.calls.length;
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(load).toHaveBeenCalledTimes(off);
    context.refreshKey++;
    rerender();
    await act(async () => {});
    expect(load).toHaveBeenCalledTimes(off + 1);
    unmount();
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(load).toHaveBeenCalledTimes(off + 1);
  });
  it("waits for completion before scheduling and aborts old tenant requests", async () => {
    let finish!: (v: string) => void;
    const slow = vi.fn(
      (signal: AbortSignal) =>
        new Promise<string>((r) => {
          finish = r;
        }),
    );
    const { result, rerender } = renderHook(
      ({ tenant }) => usePolling(tenant, slow),
      { initialProps: { tenant: "A" } },
    );
    await act(async () => vi.advanceTimersByTimeAsync(16000));
    expect(slow).toHaveBeenCalledTimes(1);
    const old = slow.mock.calls[0][0];
    const late = finish;
    rerender({ tenant: "B" });
    expect(old.aborted).toBe(true);
    await act(async () => late("A"));
    expect(result.current.data).toBeNull();
    await act(async () => finish("B"));
    expect(result.current.data).toBe("B");
  });
  it("retains prior successful timestamp on errors and stops on 401", async () => {
    const load = vi
      .fn()
      .mockResolvedValueOnce("valid")
      .mockRejectedValueOnce(new Error("401: expired"));
    const { result } = renderHook(() => usePolling("A", load));
    await act(async () => {});
    const time = result.current.fetchedAt;
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(result.current.error).toContain("401");
    expect(result.current.fetchedAt).toBe(time);
    await act(async () => vi.advanceTimersByTimeAsync(30000));
    expect(load).toHaveBeenCalledTimes(2);
  });
});
