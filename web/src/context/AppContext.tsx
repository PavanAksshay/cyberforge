import React, {
  createContext,
  useContext,
  useState,
  useEffect,
  ReactNode,
} from "react";
import { Currency, Tenant } from "../types";
import { fetchTenants, fetchTenantStatus } from "../lib/api";

export type DatePreset = "24h" | "3d" | "all" | "custom";

interface AppContextType {
  theme: "light" | "dark";
  setTheme: (t: "light" | "dark") => void;
  toggleTheme: () => void;
  currency: Currency;
  setCurrency: (c: Currency) => void;
  tenants: Tenant[];
  activeTenant: Tenant | null;
  setActiveTenant: (t: Tenant) => void;
  loadingTenants: boolean;
  tenantLoadError: string | null;
  datePreset: DatePreset;
  setDatePreset: (p: DatePreset) => void;
  startDate: string;
  endDate: string;
  setCustomDates: (start: string, end: string) => void;
  token: string;
  setToken: (tok: string) => void;
  autoRefreshInterval: number; // 0 for off, seconds otherwise
  setAutoRefreshInterval: (s: number) => void;
  refreshKey: number;
  triggerRefresh: () => void;
}

const AppContext = createContext<AppContextType | undefined>(undefined);

export function computeDateRange(
  preset: DatePreset,
  coverage?: { start: string; end: string },
): { start: string; end: string } {
  // Benchmark dataset window: 2026-10-01T00:00:00Z to 2026-10-08T00:00:00Z
  const baseStart = new Date(coverage?.start || "2026-10-01T00:00:00Z");
  const baseEnd = new Date(coverage?.end || "2026-10-08T00:00:00Z");

  let start = baseStart;
  let end = baseEnd;

  if (preset === "24h") {
    start = new Date(
      Math.max(baseStart.getTime(), baseEnd.getTime() - 86400000),
    );
    end = baseEnd; // 24h: Oct 1 - Oct 2
  } else if (preset === "3d") {
    start = new Date(
      Math.max(baseStart.getTime(), baseEnd.getTime() - 3 * 86400000),
    );
    end = baseEnd; // 3 Days: Oct 1 - Oct 4
  } else if (preset === "all") {
    start = baseStart;
    end = baseEnd; // All 7 Days: Oct 1 - Oct 8
  }

  return {
    start: start.toISOString().replace(/\.\d{3}Z$/, "Z"),
    end: end.toISOString().replace(/\.\d{3}Z$/, "Z"),
  };
}

export const AppProvider: React.FC<{ children: ReactNode }> = ({
  children,
}) => {
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    const saved = localStorage.getItem("astraroll_theme");
    if (saved === "light") return "light";
    return "dark"; // Dark & Green default
  });

  const [currency, setCurrency] = useState<Currency>("USD");
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [activeTenant, setActiveTenant] = useState<Tenant | null>(null);
  const [tenantLoadError, setTenantLoadError] = useState<string | null>(null);
  const [loadingTenants, setLoadingTenants] = useState<boolean>(true);
  const [datePreset, setDatePreset] = useState<DatePreset>("all");
  const [coverageRange, setCoverageRange] = useState<{
    start: string;
    end: string;
  }>();
  const [dates, setDates] = useState(computeDateRange("all"));
  const [token, setTokenState] = useState<string>(
    () => sessionStorage.getItem("astraroll_token") || "",
  );
  const [autoRefreshInterval, setAutoRefreshInterval] = useState<number>(0);
  const [refreshKey, setRefreshKey] = useState<number>(0);

  const setToken = (tok: string) => {
    setTokenState(tok);
    sessionStorage.setItem("astraroll_token", tok);
  };

  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
  };

  const triggerRefresh = () => {
    setRefreshKey((k) => k + 1);
  };

  useEffect(() => {
    localStorage.setItem("astraroll_theme", theme);
    if (theme === "dark") {
      document.documentElement.classList.add("dark");
    } else {
      document.documentElement.classList.remove("dark");
    }
  }, [theme]);

  const setDatePresetHandler = (p: DatePreset) => {
    setDatePreset(p);
    setDates(computeDateRange(p, coverageRange));
  };

  const setCustomDates = (start: string, end: string) => {
    setDatePreset("custom");
    setDates({ start, end });
  };

  // Load tenants on mount and when token changes (NOT on every refreshKey bump —
  // tenant list is stable; analytics data refresh is handled per-page)
  useEffect(() => {
    let isMounted = true;
    setLoadingTenants(true);
    setActiveTenant(null);
    setTenants([]);
    setTenantLoadError(null);
    fetchTenants(token)
      .then((data) => {
        if (!isMounted) return;
        setTenants(data);
        if (data.length > 0) {
          setActiveTenant(data[0]);
        }
      })
      .catch((err) => {
        if (isMounted)
          setTenantLoadError(err.message || "Tenant registry request failed");
      })
      .finally(() => {
        if (isMounted) setLoadingTenants(false);
      });

    return () => {
      isMounted = false;
    };
    // Deliberately exclude activeTenant from deps to avoid infinite loop
  }, [token]);

  useEffect(() => {
    if (!activeTenant) return;
    const controller = new AbortController();
    fetchTenantStatus(activeTenant.id, token, controller.signal)
      .then((status) => {
        if (controller.signal.aborted) return;
        const c = status.coverage.find(
          (c) => c.currency === currency && c.status === "ready",
        );
        if (c) {
          const range = { start: c.start, end: c.end };
          setCoverageRange(range);
          setDates(computeDateRange("all", range));
          setDatePreset("all");
        }
      })
      .catch(() => {});
    return () => controller.abort();
  }, [activeTenant?.id, token]);

  return (
    <AppContext.Provider
      value={{
        theme,
        setTheme,
        toggleTheme,
        currency,
        setCurrency,
        tenants,
        activeTenant,
        setActiveTenant,
        loadingTenants,
        tenantLoadError,
        datePreset,
        setDatePreset: setDatePresetHandler,
        startDate: dates.start,
        endDate: dates.end,
        setCustomDates,
        token,
        setToken,
        autoRefreshInterval,
        setAutoRefreshInterval,
        refreshKey,
        triggerRefresh,
      }}
    >
      {children}
    </AppContext.Provider>
  );
};

export const useApp = () => {
  const context = useContext(AppContext);
  if (!context) {
    throw new Error("useApp must be used within an AppProvider");
  }
  return context;
};
