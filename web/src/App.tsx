import React, { useState } from 'react';
import { AppProvider } from './context/AppContext';
import { Header } from './components/Header';
import { Sidebar, PageTab } from './components/Sidebar';
import { OverviewPage } from './pages/OverviewPage';
import { AdvancedAnalyticsPage } from './pages/AdvancedAnalyticsPage';
import { CsvIngestionPage } from './pages/CsvIngestionPage';
import { AssistantPage } from './pages/AssistantPage';
import { TenantOpsPage } from './pages/TenantOpsPage';
import { ArchitecturePage } from './pages/ArchitecturePage';

const AppContent: React.FC = () => {
  const [activeTab, setActiveTab] = useState<PageTab>('overview');

  return (
    <div className="flex flex-col min-h-screen bg-[#F8FAFC] dark:bg-[#0B1020] text-slate-900 dark:text-slate-100 transition-colors duration-200">
      <Header />

      <div className="flex flex-1 overflow-hidden">
        <Sidebar activeTab={activeTab} onTabChange={setActiveTab} />

        <main className="flex-1 overflow-y-auto p-6 lg:p-8">
          <div className="max-w-7xl mx-auto">
            {activeTab === 'overview' && <OverviewPage />}
            {activeTab === 'analytics' && <AdvancedAnalyticsPage />}
            {activeTab === 'ingestion' && <CsvIngestionPage />}
            {activeTab === 'assistant' && <AssistantPage />}
            {activeTab === 'operations' && <TenantOpsPage />}
            {activeTab === 'architecture' && <ArchitecturePage />}
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
