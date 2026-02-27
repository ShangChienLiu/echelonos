import { useState, useEffect, useCallback } from 'react';
import { Shield, RefreshCw, AlertCircle, BarChart3, Table2, Flag } from 'lucide-react';
import clsx from 'clsx';
import type { ObligationReport, ObligationRow } from './types';
import { mockReport } from './mockData';
import StatsCards from './components/StatsCards';
import ObligationTable from './components/ObligationTable';
import FlagPanel from './components/FlagPanel';
import EvidenceDrawer from './components/EvidenceDrawer';
import SummaryCharts from './components/SummaryCharts';

type Tab = 'obligations' | 'flags' | 'summary';

function App() {
  const [report, setReport] = useState<ObligationReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>('obligations');
  const [selectedObligation, setSelectedObligation] = useState<ObligationRow | null>(null);

  const fetchReport = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/report/demo-org');
      if (res.ok) {
        const data: ObligationReport = await res.json();
        setReport(data);
      } else {
        // API not available, use mock data
        console.info('API not available, using mock data');
        setReport(mockReport);
      }
    } catch {
      // Network error, use mock data
      console.info('Network error, using mock data');
      setReport(mockReport);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchReport();
  }, [fetchReport]);

  const tabs: { id: Tab; label: string; icon: typeof Table2 }[] = [
    { id: 'obligations', label: 'Obligations', icon: Table2 },
    { id: 'flags', label: 'Flags', icon: Flag },
    { id: 'summary', label: 'Summary', icon: BarChart3 },
  ];

  return (
    <div className="min-h-screen bg-slate-50 font-sans">
      {/* Header */}
      <header className="bg-slate-900 border-b border-slate-700">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center gap-3">
              <div className="bg-blue-600 p-2 rounded-lg">
                <Shield className="w-5 h-5 text-white" />
              </div>
              <div>
                <h1 className="text-lg font-bold text-white tracking-tight">
                  Echelon OS
                </h1>
                <p className="text-xs text-slate-400 -mt-0.5">
                  Obligation Intelligence Platform
                </p>
              </div>
            </div>

            <div className="flex items-center gap-4">
              {report && (
                <span className="text-xs text-slate-400 hidden sm:inline-block">
                  {report.org_name} &middot; Generated{' '}
                  {new Date(report.generated_at).toLocaleDateString('en-US', {
                    year: 'numeric',
                    month: 'short',
                    day: 'numeric',
                    hour: '2-digit',
                    minute: '2-digit',
                  })}
                </span>
              )}
              <button
                onClick={fetchReport}
                disabled={loading}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-slate-300 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-md transition-colors disabled:opacity-50"
              >
                <RefreshCw className={clsx('w-3.5 h-3.5', loading && 'animate-spin')} />
                Refresh
              </button>
            </div>
          </div>
        </div>
      </header>

      {/* Main content */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 space-y-6">
        {/* Loading */}
        {loading && !report && (
          <div className="flex items-center justify-center py-24">
            <div className="text-center">
              <RefreshCw className="w-8 h-8 text-blue-500 animate-spin mx-auto mb-3" />
              <p className="text-sm text-slate-500">Loading obligation report...</p>
            </div>
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start gap-3">
            <AlertCircle className="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
            <div>
              <p className="text-sm font-medium text-red-800">Failed to load report</p>
              <p className="text-sm text-red-600 mt-0.5">{error}</p>
            </div>
          </div>
        )}

        {/* Report loaded */}
        {report && (
          <>
            {/* Stats */}
            <StatsCards
              total={report.total_obligations}
              active={report.active_obligations}
              superseded={report.superseded_obligations}
              flagCount={report.flags.length}
            />

            {/* Tab bar */}
            <div className="border-b border-slate-200">
              <nav className="flex gap-1" aria-label="Report sections">
                {tabs.map((tab) => {
                  const Icon = tab.icon;
                  const isActive = activeTab === tab.id;
                  return (
                    <button
                      key={tab.id}
                      onClick={() => setActiveTab(tab.id)}
                      className={clsx(
                        'flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors',
                        isActive
                          ? 'border-blue-600 text-blue-600'
                          : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
                      )}
                    >
                      <Icon className="w-4 h-4" />
                      {tab.label}
                      {tab.id === 'flags' && report.flags.length > 0 && (
                        <span className="bg-red-100 text-red-700 text-xs font-semibold px-1.5 py-0.5 rounded-full">
                          {report.flags.length}
                        </span>
                      )}
                    </button>
                  );
                })}
              </nav>
            </div>

            {/* Tab content */}
            <div>
              {activeTab === 'obligations' && (
                <ObligationTable
                  obligations={report.obligations}
                  onRowClick={setSelectedObligation}
                />
              )}
              {activeTab === 'flags' && <FlagPanel flags={report.flags} />}
              {activeTab === 'summary' && <SummaryCharts summary={report.summary} />}
            </div>
          </>
        )}
      </main>

      {/* Evidence drawer */}
      <EvidenceDrawer
        obligation={selectedObligation}
        onClose={() => setSelectedObligation(null)}
      />
    </div>
  );
}

export default App;
