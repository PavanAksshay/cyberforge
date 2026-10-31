import React from 'react';
import {
  LayoutDashboard,
  TrendingUp,
  UploadCloud,
  Bot,
  Activity,
  Network,
  ShieldCheck,
  Server,
} from 'lucide-react';
import { useApp } from '../context/AppContext';

export type PageTab = 'overview' | 'analytics' | 'ingestion' | 'assistant' | 'operations' | 'architecture';

interface SidebarProps {
  activeTab: PageTab;
  onTabChange: (tab: PageTab) => void;
}

export const Sidebar: React.FC<SidebarProps> = ({ activeTab, onTabChange }) => {
  const { activeTenant } = useApp();

  const navItems: { id: PageTab; label: string; icon: React.ReactNode; badge?: string }[] = [
    {
      id: 'overview',
      label: 'Command Center',
      icon: <LayoutDashboard className="w-4 h-4" />,
    },
    {
      id: 'analytics',
      label: 'Advanced Analytics',
      icon: <TrendingUp className="w-4 h-4" />,
    },
    {
      id: 'ingestion',
      label: 'CSV Ingestion',
      icon: <UploadCloud className="w-4 h-4" />,
      badge: 'Live',
    },
    {
      id: 'assistant',
      label: 'AI Assistant',
      icon: <Bot className="w-4 h-4" />,
      badge: 'Groq',
    },
    {
      id: 'operations',
      label: 'Tenant Ops & Freshness',
      icon: <Activity className="w-4 h-4" />,
    },
    {
      id: 'architecture',
      label: 'Architecture & 1M Benchmark',
      icon: <Network className="w-4 h-4" />,
      badge: '1M',
    },
  ];

  return (
    <aside className="w-64 shrink-0 border-r bg-white/50 dark:bg-[#0B1020]/60 backdrop-blur-md border-slate-200 dark:border-slate-800 flex flex-col justify-between p-4 transition-colors">
      <div className="space-y-6">
        {/* Navigation Links */}
        <div>
          <div className="px-3 mb-2 text-[10px] font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500">
            Analytics Platform
          </div>
          <nav className="space-y-1">
            {navItems.map((item) => {
              const isActive = activeTab === item.id;
              return (
                <button
                  key={item.id}
                  onClick={() => onTabChange(item.id)}
                  className={`w-full flex items-center justify-between px-3 py-2.5 rounded-xl text-xs font-semibold transition ${
                    isActive
                      ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20'
                      : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white hover:bg-slate-100 dark:hover:bg-slate-800/60'
                  }`}
                >
                  <div className="flex items-center space-x-3">
                    <span className={isActive ? 'text-white' : 'text-slate-400 dark:text-slate-500'}>
                      {item.icon}
                    </span>
                    <span>{item.label}</span>
                  </div>
                  {item.badge && (
                    <span
                      className={`px-1.5 py-0.5 text-[10px] font-bold rounded-md ${
                        isActive
                          ? 'bg-white/20 text-white'
                          : 'bg-indigo-50 dark:bg-indigo-950/60 text-indigo-600 dark:text-indigo-400 border border-indigo-200/50 dark:border-indigo-800/50'
                      }`}
                    >
                      {item.badge}
                    </span>
                  )}
                </button>
              );
            })}
          </nav>
        </div>

        {/* Tenant DB Identity Callout */}
        {activeTenant && (
          <div className="p-3.5 rounded-2xl bg-slate-50 dark:bg-[#131B31] border border-slate-200/80 dark:border-slate-800/80">
            <div className="flex items-center space-x-2 mb-2">
              <Server className="w-3.5 h-3.5 text-indigo-500" />
              <span className="text-[11px] font-bold uppercase tracking-wider text-slate-700 dark:text-slate-300">
                Isolated Tenant DB
              </span>
            </div>
            <div className="text-xs font-semibold text-slate-900 dark:text-white truncate">
              {activeTenant.name}
            </div>
            <div className="text-[10px] font-mono text-slate-400 dark:text-slate-500 truncate mt-0.5">
              astraroll_tenant_{activeTenant.id.slice(0, 8)}
            </div>
            <div className="mt-2.5 flex items-center text-[10px] font-medium text-emerald-600 dark:text-emerald-400">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 mr-1.5 animate-pulse" />
              Physical DB Isolated
            </div>
          </div>
        )}
      </div>

      {/* Semantic Rule Guard */}
      <div className="p-3 rounded-xl bg-slate-100/80 dark:bg-slate-900/80 border border-slate-200 dark:border-slate-800 text-[11px]">
        <div className="flex items-center space-x-1.5 text-slate-700 dark:text-slate-300 font-semibold mb-1">
          <ShieldCheck className="w-3.5 h-3.5 text-indigo-500" />
          <span>Metric Semantics</span>
        </div>
        <p className="text-[10px] text-slate-500 dark:text-slate-400 leading-snug">
          Sales strictly reflect <strong className="text-slate-700 dark:text-slate-300">shipped</strong> orders under AstraRoll V2 locked semantics.
        </p>
      </div>
    </aside>
  );
};
