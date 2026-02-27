import { useState, useMemo } from 'react';
import { ChevronUp, ChevronDown, Search, Filter } from 'lucide-react';
import clsx from 'clsx';
import type { ObligationRow } from '../types';

interface ObligationTableProps {
  obligations: ObligationRow[];
  onRowClick: (obligation: ObligationRow) => void;
}

type SortKey = 'number' | 'obligation_type' | 'responsible_party' | 'source' | 'status' | 'confidence';
type SortDir = 'asc' | 'desc';

const STATUS_COLORS: Record<string, string> = {
  ACTIVE: 'bg-emerald-100 text-emerald-800',
  SUPERSEDED: 'bg-amber-100 text-amber-800',
  TERMINATED: 'bg-red-100 text-red-800',
  UNRESOLVED: 'bg-slate-100 text-slate-600',
};

function confidenceColor(c: number): string {
  if (c >= 0.9) return 'bg-emerald-500';
  if (c >= 0.8) return 'bg-amber-500';
  return 'bg-red-500';
}

function confidenceTextColor(c: number): string {
  if (c >= 0.9) return 'text-emerald-700';
  if (c >= 0.8) return 'text-amber-700';
  return 'text-red-700';
}

export default function ObligationTable({ obligations, onRowClick }: ObligationTableProps) {
  const [sortKey, setSortKey] = useState<SortKey>('number');
  const [sortDir, setSortDir] = useState<SortDir>('asc');
  const [statusFilter, setStatusFilter] = useState<string>('All');
  const [typeFilter, setTypeFilter] = useState<string>('All');
  const [searchText, setSearchText] = useState('');

  const types = useMemo(() => {
    const set = new Set(obligations.map((o) => o.obligation_type));
    return ['All', ...Array.from(set).sort()];
  }, [obligations]);

  const statuses = ['All', 'ACTIVE', 'SUPERSEDED', 'TERMINATED', 'UNRESOLVED'];

  const filtered = useMemo(() => {
    let rows = [...obligations];
    if (statusFilter !== 'All') {
      rows = rows.filter((o) => o.status === statusFilter);
    }
    if (typeFilter !== 'All') {
      rows = rows.filter((o) => o.obligation_type === typeFilter);
    }
    if (searchText.trim()) {
      const q = searchText.toLowerCase();
      rows = rows.filter((o) => o.obligation_text.toLowerCase().includes(q));
    }
    rows.sort((a, b) => {
      const aVal = a[sortKey];
      const bVal = b[sortKey];
      if (typeof aVal === 'number' && typeof bVal === 'number') {
        return sortDir === 'asc' ? aVal - bVal : bVal - aVal;
      }
      const aStr = String(aVal ?? '');
      const bStr = String(bVal ?? '');
      return sortDir === 'asc' ? aStr.localeCompare(bStr) : bStr.localeCompare(aStr);
    });
    return rows;
  }, [obligations, statusFilter, typeFilter, searchText, sortKey, sortDir]);

  function handleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc');
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  }

  function SortIcon({ column }: { column: SortKey }) {
    if (sortKey !== column) {
      return <ChevronUp className="w-3.5 h-3.5 text-slate-300 ml-1 inline-block" />;
    }
    return sortDir === 'asc' ? (
      <ChevronUp className="w-3.5 h-3.5 text-slate-600 ml-1 inline-block" />
    ) : (
      <ChevronDown className="w-3.5 h-3.5 text-slate-600 ml-1 inline-block" />
    );
  }

  return (
    <div className="bg-white rounded-lg shadow-sm border border-slate-200 overflow-hidden">
      {/* Toolbar */}
      <div className="p-4 border-b border-slate-200 space-y-3 sm:space-y-0 sm:flex sm:items-center sm:gap-3">
        {/* Search */}
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
          <input
            type="text"
            placeholder="Search obligations..."
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            className="w-full pl-9 pr-3 py-2 text-sm border border-slate-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 bg-white"
          />
        </div>

        <div className="flex items-center gap-2">
          <Filter className="w-4 h-4 text-slate-400 shrink-0" />
          {/* Status filter */}
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="text-sm border border-slate-300 rounded-md px-2.5 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white text-slate-700"
          >
            {statuses.map((s) => (
              <option key={s} value={s}>
                {s === 'All' ? 'All Statuses' : s}
              </option>
            ))}
          </select>

          {/* Type filter */}
          <select
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
            className="text-sm border border-slate-300 rounded-md px-2.5 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white text-slate-700"
          >
            {types.map((t) => (
              <option key={t} value={t}>
                {t === 'All' ? 'All Types' : t}
              </option>
            ))}
          </select>
        </div>

        {/* Result count */}
        <span className="text-sm text-slate-500 shrink-0">
          {filtered.length} of {obligations.length} obligations
        </span>
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-slate-50 text-left">
              <th
                className="px-4 py-3 font-semibold text-slate-600 cursor-pointer select-none whitespace-nowrap"
                onClick={() => handleSort('number')}
              >
                # <SortIcon column="number" />
              </th>
              <th className="px-4 py-3 font-semibold text-slate-600 min-w-[280px]">
                Obligation
              </th>
              <th
                className="px-4 py-3 font-semibold text-slate-600 cursor-pointer select-none whitespace-nowrap"
                onClick={() => handleSort('obligation_type')}
              >
                Type <SortIcon column="obligation_type" />
              </th>
              <th
                className="px-4 py-3 font-semibold text-slate-600 cursor-pointer select-none whitespace-nowrap"
                onClick={() => handleSort('responsible_party')}
              >
                Owner <SortIcon column="responsible_party" />
              </th>
              <th
                className="px-4 py-3 font-semibold text-slate-600 cursor-pointer select-none whitespace-nowrap"
                onClick={() => handleSort('source')}
              >
                Source <SortIcon column="source" />
              </th>
              <th
                className="px-4 py-3 font-semibold text-slate-600 cursor-pointer select-none whitespace-nowrap"
                onClick={() => handleSort('status')}
              >
                Status <SortIcon column="status" />
              </th>
              <th
                className="px-4 py-3 font-semibold text-slate-600 cursor-pointer select-none whitespace-nowrap"
                onClick={() => handleSort('confidence')}
              >
                Confidence <SortIcon column="confidence" />
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-4 py-12 text-center text-slate-400">
                  No obligations match the current filters.
                </td>
              </tr>
            ) : (
              filtered.map((obl) => (
                <tr
                  key={obl.number}
                  onClick={() => onRowClick(obl)}
                  className="hover:bg-slate-50 cursor-pointer transition-colors"
                >
                  <td className="px-4 py-3 text-slate-500 font-mono text-xs">{obl.number}</td>
                  <td className="px-4 py-3 text-slate-800 leading-snug">
                    <span className="line-clamp-2">{obl.obligation_text}</span>
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap">
                    <span className="inline-block bg-slate-100 text-slate-700 text-xs font-medium px-2 py-0.5 rounded">
                      {obl.obligation_type}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-700 whitespace-nowrap text-xs">
                    {obl.responsible_party}
                  </td>
                  <td className="px-4 py-3 text-slate-500 text-xs whitespace-nowrap max-w-[180px] truncate">
                    {obl.source}
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap">
                    <span
                      className={clsx(
                        'inline-block text-xs font-semibold px-2.5 py-1 rounded-full',
                        STATUS_COLORS[obl.status] ?? 'bg-slate-100 text-slate-600'
                      )}
                    >
                      {obl.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap">
                    <div className="flex items-center gap-2">
                      <div className="w-16 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                        <div
                          className={clsx('h-full rounded-full', confidenceColor(obl.confidence))}
                          style={{ width: `${obl.confidence * 100}%` }}
                        />
                      </div>
                      <span className={clsx('text-xs font-medium', confidenceTextColor(obl.confidence))}>
                        {(obl.confidence * 100).toFixed(0)}%
                      </span>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
