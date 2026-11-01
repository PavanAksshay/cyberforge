import React, { useEffect, useState } from "react";
import { useApp, AppProvider } from "./context/AppContext";
import { Header } from "./components/Header";
import { Sidebar, PageTab } from "./components/Sidebar";
import { OverviewPage } from "./pages/OverviewPage";
import { AdvancedAnalyticsPage } from "./pages/AdvancedAnalyticsPage";
import { CsvIngestionPage } from "./pages/CsvIngestionPage";
import { AssistantPage } from "./pages/AssistantPage";
import { TenantOpsPage } from "./pages/TenantOpsPage";
import { ArchitecturePage } from "./pages/ArchitecturePage";
import { PortfolioPage } from "./pages/PortfolioPage";

const AppContent: React.FC = () => {
  const { tenantLoadError, activeTenant, currency, startDate, endDate } =
    useApp();
  const scopeKey = `${activeTenant?.id}:${currency}:${startDate}:${endDate}`;
  const [activeTab, setActiveTab] = useState<PageTab>("overview");
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "auto" });
  }, [activeTab]);

  return (
    <div className="flex flex-col min-h-screen bg-transparent text-slate-100 transition-colors duration-200">
      <Header portfolio={activeTab === "portfolio"} />

      <div className="flex flex-col lg:flex-row flex-1 min-w-0">
        <Sidebar activeTab={activeTab} onTabChange={setActiveTab} />

        <main className="flex-1 min-w-0 w-full overflow-y-auto p-4 lg:p-8">
          <div className="max-w-7xl mx-auto">
            {tenantLoadError && (
              <p
                role="alert"
                className="p-4 rounded-xl bg-rose-950 text-rose-300"
              >
                {tenantLoadError}. Open API Bearer Token to authenticate with a
                valid expiring key.
              </p>
            )}
            {activeTab === "overview" && <OverviewPage />}
            {activeTab === "portfolio" && (
              <PortfolioPage onDrillDown={() => setActiveTab("overview")} />
            )}
            {activeTab === "analytics" && <AdvancedAnalyticsPage />}
            {activeTab === "ingestion" && <CsvIngestionPage />}
            {activeTab === "assistant" && <AssistantPage key={scopeKey} />}
            {activeTab === "operations" && <TenantOpsPage />}
            {activeTab === "architecture" && <ArchitecturePage />}
          </div>
        </main>
      </div>
    </div>
  );
};

export const App: React.FC = () => {
  return (
    <AppProvider>
      <AppContent />
    </AppProvider>
  );
};

export default App;
