import { useState, useEffect, useCallback } from 'react';
import { Shield, RefreshCw, AlertCircle, BarChart3, Table2, Flag, ChevronDown } from 'lucide-react';
import clsx from 'clsx';
import type { ObligationReport, ObligationRow } from './types';
import { mockReport } from './mockData';
import StatsCards from './components/StatsCards';
import ObligationTable from './components/ObligationTable';
import FlagPanel from './components/FlagPanel';
import EvidenceDrawer from './components/EvidenceDrawer';
import SummaryCharts from './components/SummaryCharts';
import DocumentUpload from './components/DocumentUpload';

type Tab = 'obligations' | 'flags' | 'summary';

interface OrgOption {
  id: string;
  name: string;
}

function App() {
  const [report, setReport] = useState<ObligationReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>('obligations');
  const [selectedObligation, setSelectedObligation] = useState<ObligationRow | null>(null);
  const [organizations, setOrganizations] = useState<OrgOption[]>([]);
  const [selectedOrg, setSelectedOrg] = useState<string>('');

  const fetchOrganizations = useCallback(async () => {
    try {
      const res = await fetch('/api/organizations');
      if (res.ok) {
        const orgs: OrgOption[] = await res.json();
        setOrganizations(orgs);
        return orgs;
      }
    } catch {
      // API not available
    }
    return [];
  }, []);

  // Fetch available organizations on mount
  useEffect(() => {
    (async () => {
      const orgs = await fetchOrganizations();
      if (orgs.length > 0) {
        setSelectedOrg(orgs[0].name);
      }
    })();
  }, [fetchOrganizations]);

  const fetchReport = useCallback(async () => {
    if (!selectedOrg) {
      setReport(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/report/${encodeURIComponent(selectedOrg)}`);
      if (res.ok) {
        const data: ObligationReport = await res.json();
        setReport(data);
      } else {
        console.info('API not available, using mock data');
        setReport(mockReport);
      }
    } catch {
      console.info('Network error, using mock data');
      setReport(mockReport);
    } finally {
      setLoading(false);
    }
  }, [selectedOrg]);

  useEffect(() => {
    fetchReport();
  }, [fetchReport]);

  const handleRefresh = useCallback(async () => {
    setLoading(true);
    try {
      const orgs = await fetchOrganizations();
      if (orgs.length > 0) {
        if (!selectedOrg || !orgs.find((o) => o.name === selectedOrg)) {
          setSelectedOrg(orgs[0].name);
          // fetchReport will auto-fire via useEffect when selectedOrg changes.
          return;
        }
      }
      await fetchReport();
    } finally {
      setLoading(false);
    }
  }, [fetchOrganizations, fetchReport, selectedOrg]);

  const refreshAfterUpload = useCallback(async () => {
    const orgs = await fetchOrganizations();
    if (orgs.length > 0) {
      if (!selectedOrg || !orgs.find((o) => o.name === selectedOrg)) {
        setSelectedOrg(orgs[0].name);
        return;
      }
    }
    await fetchReport();
  }, [fetchOrganizations, fetchReport, selectedOrg]);

  const handleClearDatabase = useCallback(async () => {
    await fetch('/api/database', { method: 'DELETE' });
    setOrganizations([]);
    setSelectedOrg('');
    setReport(null);
    setLoading(false);
    setError(null);
  }, []);

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
              {/* Organization selector */}
              {organizations.length > 0 && (
                <div className="relative">
                  <select
                    value={selectedOrg}
                    onChange={(e) => setSelectedOrg(e.target.value)}
                    className="appearance-none bg-slate-800 text-slate-300 text-xs font-medium pl-3 pr-7 py-1.5 rounded-md border border-slate-600 hover:border-slate-500 focus:outline-none focus:ring-1 focus:ring-blue-500 cursor-pointer"
                  >
                    {organizations.map((org) => (
                      <option key={org.id} value={org.name}>
                        {org.name}
                      </option>
                    ))}
                  </select>
                  <ChevronDown className="w-3 h-3 text-slate-400 absolute right-2 top-1/2 -translate-y-1/2 pointer-events-none" />
                </div>
              )}

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
                type="button"
                onClick={handleRefresh}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-slate-300 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-md transition-colors"
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
        {/* Document Upload */}
        <DocumentUpload
          onUploadComplete={refreshAfterUpload}
          onClearDatabase={handleClearDatabase}
        />

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
