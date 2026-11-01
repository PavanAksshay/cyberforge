import { useEffect, useRef, useState } from "react";
import { useApp } from "../context/AppContext";

// One request group per mounted data view. The next timer starts after completion.
// Abort on scope/manual refresh/unmount. A generation fence also rejects late responses.
export function usePolling<T>(
  scope: string,
  load: (signal: AbortSignal) => Promise<T>,
) {
  const { autoRefreshInterval, refreshKey } = useApp();
  const loader = useRef(load);
  loader.current = load;
  const [state, setState] = useState<{
    data: T | null;
    error: string | null;
    loading: boolean;
    fetchedAt: Date | null;
    nextAt: number | null;
  }>({ data: null, error: null, loading: true, fetchedAt: null, nextAt: null });
  const previousScope = useRef(scope);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    if (previousScope.current !== scope) {
      previousScope.current = scope;
      setState({
        data: null,
        error: null,
        loading: true,
        fetchedAt: null,
        nextAt: null,
      });
    }
    async function run() {
      setState((s) => ({ ...s, loading: true, nextAt: null }));
      let authFailed = false;
      try {
        const data = await loader.current(controller.signal);
        if (active)
          setState({
            data,
            error: null,
            loading: false,
            fetchedAt: new Date(),
            nextAt: null,
          });
      } catch (e) {
        if (!active) return;
        const error = e instanceof Error ? e.message : String(e);
        authFailed = /\b(401|403)\b/.test(error);
        setState((s) => ({ ...s, error, loading: false }));
      }
      if (active && autoRefreshInterval > 0 && !authFailed) {
        const nextAt = Date.now() + autoRefreshInterval * 1000;
        setState((s) => ({ ...s, nextAt }));
        timer = setTimeout(run, autoRefreshInterval * 1000);
      }
    }
    void run();
    return () => {
      active = false;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [scope, autoRefreshInterval, refreshKey]);
  return previousScope.current === scope
    ? state
    : {
        ...state,
        data: null,
        error: null,
        fetchedAt: null,
        loading: true,
        nextAt: null,
      };
}
