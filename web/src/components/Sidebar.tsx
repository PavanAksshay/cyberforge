import React, { useState } from 'react';
import {
  LayoutDashboard,
  TrendingUp,
  UploadCloud,
  Bot,
  Activity,
  Network,
  Database,
  Terminal,
  Copy,
  Check,
  Code,
  ShieldCheck,
  CheckCircle2,
} from 'lucide-react';
import { useApp } from '../context/AppContext';

export type PageTab = 'overview' | 'analytics' | 'ingestion' | 'assistant' | 'operations' | 'architecture';

interface SidebarProps {
  activeTab: PageTab;
  onTabChange: (tab: PageTab) => void;
}

export const Sidebar: React.FC<SidebarProps> = ({ activeTab, onTabChange }) => {
  const { activeTenant, currency, startDate, endDate, datePreset } = useApp();
  const [copied, setCopied] = useState<boolean>(false);
  const [queryMode, setQueryMode] = useState<'rollup' | 'raw' | 'psql'>('rollup');

  const navItems: { id: PageTab; label: string; icon: React.ReactNode; badge?: string }[] = [
    {
      id: 'overview',
      label: 'Command Center',
      icon: <LayoutDashboard className="w-5 h-5" />,
    },
    {
      id: 'analytics',
      label: 'Advanced Analytics',
      icon: <TrendingUp className="w-5 h-5" />,
    },
    {
      id: 'ingestion',
      label: 'CSV Ingestion',
      icon: <UploadCloud className="w-5 h-5" />,
      badge: 'Live',
    },
    {
      id: 'assistant',
      label: 'AI Assistant',
      icon: <Bot className="w-5 h-5" />,
      badge: 'Groq',
    },
    {
      id: 'operations',
      label: 'Tenant Ops & Freshness',
      icon: <Activity className="w-5 h-5" />,
    },
    {
      id: 'architecture',
      label: 'Architecture & 10M Scale',
      icon: <Network className="w-5 h-5" />,
      badge: '10M',
    },
  ];

  // Format tenant name cleanly (e.g. "Synthetic 33aa... / 33" -> "Enterprise Tenant #33")
  const formatTenantDisplayName = (name?: string) => {
    if (!name) return 'Primary Tenant';
    const match = name.match(/\/ (\d+)$/);
    if (match) {
      return `Enterprise Tenant #${match[1]}`;
    }
    return name;
  };

  // Physical database name in PostgreSQL 55433
  const rawIdClean = activeTenant ? activeTenant.id.replace(/-/g, '') : '';
  const tenantDbName = activeTenant ? `v2bench_78b1ee4a_${rawIdClean}` : 'v2bench_control_78b1ee4a';
  const tenantUuid = activeTenant ? activeTenant.id : '';

  // Format start and end timestamps for SQL
  const formattedStart = startDate ? startDate.replace('T', ' ').replace('Z', '+00') : '2026-10-01 00:00:00+00';
  const formattedEnd = endDate ? endDate.replace('T', ' ').replace('Z', '+00') : '2026-10-08 00:00:00+00';

  // Dynamic SQL Generation based on current UI selection (including explicit tenant_id)
  const rollupSql = `SELECT 
  SUM(order_count) AS total_orders, 
  SUM(sales_amount) AS total_shipped_sales 
FROM hourly_summary_aggregates 
WHERE tenant_id = '${tenantUuid}'
  AND currency = '${currency}' 
  AND bucket_start >= '${formattedStart}' 
  AND bucket_start < '${formattedEnd}';`;

  const rawSql = `SELECT 
  COUNT(*) AS total_orders, 
  COALESCE(SUM(CASE WHEN status='shipped' THEN amount ELSE 0 END), 0) AS total_shipped_sales 
FROM orders 
WHERE tenant_id = '${tenantUuid}'
  AND currency = '${currency}' 
  AND created_at >= '${formattedStart}' 
  AND created_at < '${formattedEnd}';`;

  const psqlOneLiner = `psql -h 127.0.0.1 -p 55433 -U astraroll -d ${tenantDbName} -c "SELECT SUM(order_count) AS total_orders, SUM(sales_amount) AS total_shipped_sales FROM hourly_summary_aggregates WHERE tenant_id='${tenantUuid}' AND currency='${currency}' AND bucket_start >= '${formattedStart}' AND bucket_start < '${formattedEnd}';"`;

  const activeQueryText = queryMode === 'rollup' ? rollupSql : queryMode === 'raw' ? rawSql : psqlOneLiner;

  const handleCopy = () => {
    navigator.clipboard.writeText(activeQueryText);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <aside className="w-84 sm:w-92 shrink-0 border-r bg-[#051c15]/95 backdrop-blur-md border-[#0f4838] flex flex-col justify-between p-4 overflow-y-auto transition-colors space-y-6">
      <div className="space-y-6">
        {/* Navigation Section */}
        <div>
          <div className="px-3 mb-3 text-xs font-bold uppercase tracking-wider text-emerald-400/80">
            Analytics Platform
          </div>
          <nav className="space-y-1.5">
            {navItems.map((item) => {
              const isActive = activeTab === item.id;
              return (
                <button
                  key={item.id}
                  onClick={() => onTabChange(item.id)}
                  className={`w-full flex items-center justify-between px-3.5 py-2.5 rounded-2xl text-sm font-semibold transition-all ${
                    isActive
                      ? 'bg-gradient-to-r from-emerald-600 to-emerald-700 text-white shadow-lg shadow-emerald-900/30'
                      : 'text-slate-300 hover:text-white hover:bg-[#092e23] border border-transparent hover:border-[#104d3b]'
                  }`}
                >
                  <div className="flex items-center space-x-3">
                    <span className={isActive ? 'text-white' : 'text-emerald-400'}>
                      {item.icon}
                    </span>
                    <span className="font-semibold text-sm">{item.label}</span>
                  </div>
                  {item.badge && (
                    <span
                      className={`px-2 py-0.5 text-xs font-bold rounded-lg ${
                        isActive
                          ? 'bg-white/20 text-white'
                          : 'bg-emerald-950/80 text-emerald-300 border border-emerald-800'
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

        {/* Dynamic SQL Verification Card (Live Cross-Check Generator) */}
        {activeTenant && (
          <div className="p-4 rounded-3xl bg-[#062118] border border-[#0d3d2f] shadow-xl space-y-3.5">
            <div className="flex items-center justify-between pb-2 border-b border-[#0d3d2f]/60">
              <div className="flex items-center space-x-2">
                <Code className="w-4 h-4 text-emerald-400" />
                <span className="text-xs sm:text-sm font-black uppercase tracking-wider text-emerald-300">
                  Live SQL Cross-Verify
                </span>
              </div>
              <button
                onClick={handleCopy}
                className="flex items-center space-x-1.5 px-3 py-1.5 rounded-xl bg-emerald-950 hover:bg-emerald-900 text-emerald-300 border border-emerald-700 text-xs font-bold font-mono transition shadow-sm"
                title="Copy SQL command to clipboard"
              >
                {copied ? (
                  <>
                    <Check className="w-3.5 h-3.5 text-emerald-400" />
                    <span>Copied!</span>
                  </>
                ) : (
                  <>
                    <Copy className="w-3.5 h-3.5 text-slate-300" />
                    <span>Copy SQL</span>
                  </>
                )}
              </button>
            </div>

            {/* Target Database & Tenant ID Info */}
            <div className="p-3 rounded-2xl bg-[#03150f] border border-[#092c21] space-y-1.5">
              <div className="text-xs font-black uppercase tracking-wider text-slate-300">
                Target DB:
              </div>
              <div className="text-xs sm:text-sm font-mono font-bold text-emerald-300 break-all leading-snug" title={tenantDbName}>
                {tenantDbName}
              </div>
              <div className="text-[11px] font-mono text-slate-400 flex items-center justify-between pt-1 border-t border-[#0d3d2f]/50">
                <span>Tenant UUID:</span>
                <span className="text-emerald-400 font-semibold">{tenantUuid.slice(0, 18)}...</span>
              </div>
            </div>

            {/* Mode Selector Tabs (Substantially Larger Text) */}
            <div className="grid grid-cols-3 gap-1.5 p-1 rounded-xl bg-[#03150f] border border-[#092c21] text-xs font-bold">
              <button
                onClick={() => setQueryMode('rollup')}
                className={`py-2 px-1 rounded-lg text-center transition ${
                  queryMode === 'rollup' ? 'bg-emerald-600 text-white shadow-md' : 'text-slate-400 hover:text-white hover:bg-emerald-950/40'
                }`}
              >
                Rollup
              </button>
              <button
                onClick={() => setQueryMode('raw')}
                className={`py-2 px-1 rounded-lg text-center transition ${
                  queryMode === 'raw' ? 'bg-emerald-600 text-white shadow-md' : 'text-slate-400 hover:text-white hover:bg-emerald-950/40'
                }`}
              >
                Raw Orders
              </button>
              <button
                onClick={() => setQueryMode('psql')}
                className={`py-2 px-1 rounded-lg text-center transition ${
                  queryMode === 'psql' ? 'bg-emerald-600 text-white shadow-md' : 'text-slate-400 hover:text-white hover:bg-emerald-950/40'
                }`}
              >
                psql CLI
              </button>
            </div>

            {/* Live Generated SQL Code Block (Larger & Highly Legible) */}
            <div className="relative group">
              <pre className="p-3.5 rounded-2xl bg-[#03140e] border border-[#0a3528] text-xs sm:text-sm font-mono text-emerald-300 leading-relaxed overflow-x-auto max-h-48 select-all scrollbar-thin">
                {activeQueryText}
              </pre>
            </div>

            {/* Live Active Context Badges */}
            <div className="text-xs text-slate-300 leading-tight flex items-center justify-between font-mono pt-1">
              <span>Preset: <strong className="text-emerald-400 uppercase font-bold">{datePreset}</strong></span>
              <span>Currency: <strong className="text-emerald-400 font-bold">{currency}</strong></span>
            </div>
          </div>
        )}

        {/* Tenant Database Proof Card */}
        {activeTenant && (
          <div className="p-3.5 rounded-2xl bg-[#082920] border border-[#114f3c] shadow-sm space-y-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2">
                <Database className="w-3.5 h-3.5 text-emerald-400" />
                <span className="text-xs font-bold uppercase tracking-wider text-emerald-300">
                  Active Physical Tenant
                </span>
              </div>
              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-bold bg-emerald-950 text-emerald-300 border border-emerald-800">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 mr-1.5 animate-pulse" />
                Port 55433
              </span>
            </div>

            <div>
              <div className="text-sm font-bold text-white truncate">
                {formatTenantDisplayName(activeTenant.name)}
              </div>
              <div className="text-xs font-mono text-emerald-400/90 truncate mt-0.5">
                v2bench_{activeTenant.id.slice(0, 8)}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Metric Integrity Guard */}
      <div className="p-3.5 rounded-2xl bg-[#07241c] border border-[#0f4536] text-xs">
        <div className="flex items-center space-x-1.5 text-emerald-300 font-semibold mb-1">
          <ShieldCheck className="w-4 h-4 text-emerald-400" />
          <span className="font-extrabold text-xs uppercase tracking-wider">100% SQL Mathematical Parity</span>
        </div>
        <p className="text-xs text-slate-300 leading-relaxed">
          Paste the dynamic SQL directly into PostgreSQL to verify identical counts and dollars.
        </p>
      </div>
    </aside>
  );
};
