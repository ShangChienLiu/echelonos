import { X, FileText, Users, Scale, Clock, MapPin, GitBranch } from 'lucide-react';
import clsx from 'clsx';
import type { ObligationRow, AmendmentHistoryEntry } from '../types';

interface EvidenceDrawerProps {
  obligation: ObligationRow | null;
  onClose: () => void;
}

const STATUS_COLORS: Record<string, string> = {
  ACTIVE: 'bg-emerald-100 text-emerald-800',
  SUPERSEDED: 'bg-amber-100 text-amber-800',
  TERMINATED: 'bg-red-100 text-red-800',
  UNRESOLVED: 'bg-slate-100 text-slate-600',
};

function confidenceBarColor(c: number): string {
  if (c >= 0.9) return 'bg-emerald-500';
  if (c >= 0.8) return 'bg-amber-500';
  return 'bg-red-500';
}

function confidenceLabelColor(c: number): string {
  if (c >= 0.9) return 'text-emerald-700';
  if (c >= 0.8) return 'text-amber-700';
  return 'text-red-700';
}

export default function EvidenceDrawer({ obligation, onClose }: EvidenceDrawerProps) {
  if (!obligation) return null;

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/30 z-40 transition-opacity"
        onClick={onClose}
      />

      {/* Drawer */}
      <div className="fixed right-0 top-0 h-full w-full max-w-lg bg-white shadow-2xl z-50 flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-200 bg-slate-50 shrink-0">
          <div>
            <h2 className="text-base font-semibold text-slate-800">
              Obligation #{obligation.number}
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">Evidence &amp; Details</p>
          </div>
          <button
            onClick={onClose}
            className="p-2 hover:bg-slate-200 rounded-lg transition-colors"
            aria-label="Close drawer"
          >
            <X className="w-5 h-5 text-slate-500" />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {/* Status and Confidence */}
          <div className="flex items-center gap-3">
            <span
              className={clsx(
                'text-xs font-semibold px-3 py-1 rounded-full',
                STATUS_COLORS[obligation.status] ?? 'bg-slate-100 text-slate-600'
              )}
            >
              {obligation.status}
            </span>
            <div className="flex items-center gap-2">
              <div className="w-20 h-2 bg-slate-100 rounded-full overflow-hidden">
                <div
                  className={clsx('h-full rounded-full', confidenceBarColor(obligation.confidence))}
                  style={{ width: `${obligation.confidence * 100}%` }}
                />
              </div>
              <span className={clsx('text-sm font-semibold', confidenceLabelColor(obligation.confidence))}>
                {(obligation.confidence * 100).toFixed(0)}% confidence
              </span>
            </div>
          </div>

          {/* Obligation text */}
          <div>
            <h3 className="text-sm font-semibold text-slate-500 uppercase tracking-wider mb-2">
              Obligation Text
            </h3>
            <p className="text-sm text-slate-800 leading-relaxed bg-slate-50 rounded-lg p-4 border border-slate-200">
              {obligation.obligation_text}
            </p>
          </div>

          {/* Source clause */}
          {obligation.source_clause && (
            <div>
              <h3 className="text-sm font-semibold text-slate-500 uppercase tracking-wider mb-2">
                Source Clause
              </h3>
              <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
                <p className="text-sm text-slate-700 leading-relaxed italic">
                  "{obligation.source_clause}"
                </p>
                <div className="flex items-center gap-4 mt-3 text-xs text-slate-500">
                  {obligation.doc_filename && (
                    <span className="flex items-center gap-1">
                      <FileText className="w-3.5 h-3.5" />
                      {obligation.doc_filename}
                    </span>
                  )}
                  {obligation.source_page !== undefined && (
                    <span className="flex items-center gap-1">
                      <MapPin className="w-3.5 h-3.5" />
                      Page {obligation.source_page}
                    </span>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* Amendment History */}
          {obligation.amendment_history && obligation.amendment_history.length > 0 && (
            <div>
              <h3 className="text-sm font-semibold text-slate-500 uppercase tracking-wider mb-2">
                <span className="flex items-center gap-1.5">
                  <GitBranch className="w-4 h-4" />
                  Amendment History
                </span>
              </h3>
              <div className="space-y-3">
                {obligation.amendment_history.map((entry, idx) => (
                  <AmendmentHistoryCard key={idx} entry={entry} />
                ))}
              </div>
            </div>
          )}

          {/* Details grid */}
          <div>
            <h3 className="text-sm font-semibold text-slate-500 uppercase tracking-wider mb-3">
              Details
            </h3>
            <div className="grid grid-cols-2 gap-3">
              <DetailCard
                icon={<Scale className="w-4 h-4 text-blue-500" />}
                label="Type"
                value={obligation.obligation_type}
              />
              <DetailCard
                icon={<Users className="w-4 h-4 text-indigo-500" />}
                label="Responsible Party"
                value={obligation.responsible_party}
              />
              <DetailCard
                icon={<Users className="w-4 h-4 text-violet-500" />}
                label="Counterparty"
                value={obligation.counterparty}
              />
              <DetailCard
                icon={<FileText className="w-4 h-4 text-slate-500" />}
                label="Source"
                value={obligation.source}
              />
              {obligation.frequency && (
                <DetailCard
                  icon={<Clock className="w-4 h-4 text-teal-500" />}
                  label="Frequency"
                  value={obligation.frequency}
                />
              )}
              {obligation.deadline && (
                <DetailCard
                  icon={<Clock className="w-4 h-4 text-orange-500" />}
                  label="Deadline"
                  value={obligation.deadline}
                />
              )}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

function DetailCard({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="bg-slate-50 rounded-lg p-3 border border-slate-200">
      <div className="flex items-center gap-1.5 mb-1">
        {icon}
        <span className="text-xs font-medium text-slate-500">{label}</span>
      </div>
      <p className="text-sm font-medium text-slate-800 break-words">{value}</p>
    </div>
  );
}

const ACTION_BADGE_COLORS: Record<string, string> = {
  REPLACE: 'bg-red-100 text-red-800',
  MODIFY: 'bg-amber-100 text-amber-800',
  DELETE: 'bg-slate-800 text-white',
  UNCHANGED: 'bg-slate-100 text-slate-600',
};

function AmendmentHistoryCard({ entry }: { entry: AmendmentHistoryEntry }) {
  return (
    <div className="bg-amber-50 border border-amber-200 rounded-lg p-4">
      <div className="flex items-center justify-between mb-2">
        <span
          className={clsx(
            'text-xs font-bold px-2.5 py-0.5 rounded-full',
            ACTION_BADGE_COLORS[entry.action] ?? 'bg-slate-100 text-slate-600'
          )}
        >
          {entry.action}
        </span>
        {entry.doc_filename && (
          <span className="flex items-center gap-1 text-xs text-slate-500">
            <FileText className="w-3.5 h-3.5" />
            {entry.doc_filename}
            {entry.amendment_number !== undefined && (
              <span className="text-slate-400 ml-1">(Amd #{entry.amendment_number})</span>
            )}
          </span>
        )}
      </div>
      {entry.reasoning && (
        <p className="text-sm text-slate-700 leading-relaxed mb-2">
          {entry.reasoning}
        </p>
      )}
      <div className="flex items-center gap-2">
        <div className="w-16 h-1.5 bg-amber-100 rounded-full overflow-hidden">
          <div
            className={clsx('h-full rounded-full', confidenceBarColor(entry.confidence))}
            style={{ width: `${entry.confidence * 100}%` }}
          />
        </div>
        <span className={clsx('text-xs font-medium', confidenceLabelColor(entry.confidence))}>
          {(entry.confidence * 100).toFixed(0)}%
        </span>
      </div>
    </div>
  );
}
