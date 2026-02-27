import { useMemo } from 'react';
import clsx from 'clsx';
import type { FlagItem } from '../types';

interface FlagPanelProps {
  flags: FlagItem[];
}

const SEVERITY_ORDER: FlagItem['severity'][] = ['RED', 'ORANGE', 'YELLOW', 'WHITE'];

const SEVERITY_DOT: Record<string, string> = {
  RED: 'bg-red-500',
  ORANGE: 'bg-orange-500',
  YELLOW: 'bg-yellow-400',
  WHITE: 'bg-slate-300',
};

const SEVERITY_BG: Record<string, string> = {
  RED: 'bg-red-50 border-red-200',
  ORANGE: 'bg-orange-50 border-orange-200',
  YELLOW: 'bg-yellow-50 border-yellow-200',
  WHITE: 'bg-slate-50 border-slate-200',
};

const SEVERITY_LABEL_BG: Record<string, string> = {
  RED: 'bg-red-100 text-red-800',
  ORANGE: 'bg-orange-100 text-orange-800',
  YELLOW: 'bg-yellow-100 text-yellow-800',
  WHITE: 'bg-slate-100 text-slate-600',
};

const FLAG_TYPE_COLOR: Record<string, string> = {
  LOW_CONFIDENCE: 'bg-purple-100 text-purple-700',
  UNRESOLVED: 'bg-red-100 text-red-700',
  UNLINKED: 'bg-blue-100 text-blue-700',
  AMBIGUOUS: 'bg-amber-100 text-amber-700',
  UNVERIFIED: 'bg-slate-100 text-slate-600',
};

export default function FlagPanel({ flags }: FlagPanelProps) {
  const grouped = useMemo(() => {
    const groups = new Map<string, FlagItem[]>();
    for (const sev of SEVERITY_ORDER) {
      groups.set(sev, []);
    }
    for (const flag of flags) {
      const arr = groups.get(flag.severity) ?? [];
      arr.push(flag);
      groups.set(flag.severity, arr);
    }
    return groups;
  }, [flags]);

  return (
    <div className="bg-white rounded-lg shadow-sm border border-slate-200 overflow-hidden">
      <div className="p-4 border-b border-slate-200">
        <h3 className="text-base font-semibold text-slate-800">Flags &amp; Warnings</h3>
        <p className="text-sm text-slate-500 mt-0.5">
          {flags.length} flag{flags.length !== 1 ? 's' : ''} requiring attention
        </p>
      </div>

      <div className="p-4 space-y-4">
        {SEVERITY_ORDER.map((severity) => {
          const items = grouped.get(severity) ?? [];
          if (items.length === 0) return null;

          return (
            <div key={severity}>
              {/* Severity header */}
              <div className="flex items-center gap-2 mb-2">
                <span className={clsx('w-2.5 h-2.5 rounded-full', SEVERITY_DOT[severity])} />
                <span className="text-sm font-semibold text-slate-700">{severity}</span>
                <span
                  className={clsx(
                    'text-xs font-medium px-2 py-0.5 rounded-full',
                    SEVERITY_LABEL_BG[severity]
                  )}
                >
                  {items.length}
                </span>
              </div>

              {/* Flag cards */}
              <div className="space-y-2 ml-5">
                {items.map((flag, idx) => (
                  <div
                    key={`${flag.entity_id}-${idx}`}
                    className={clsx(
                      'border rounded-lg p-3 transition-colors',
                      SEVERITY_BG[flag.severity]
                    )}
                  >
                    <div className="flex items-start gap-2">
                      <span
                        className={clsx(
                          'text-xs font-medium px-2 py-0.5 rounded shrink-0 mt-0.5',
                          FLAG_TYPE_COLOR[flag.flag_type] ?? 'bg-slate-100 text-slate-600'
                        )}
                      >
                        {flag.flag_type.replace(/_/g, ' ')}
                      </span>
                      <p className="text-sm text-slate-700 leading-relaxed">{flag.message}</p>
                    </div>
                    <div className="mt-2 flex items-center gap-3 text-xs text-slate-500">
                      <span>
                        Entity: <span className="font-medium text-slate-600">{flag.entity_type}</span>
                      </span>
                      <span className="text-slate-300">|</span>
                      <span>
                        ID: <span className="font-mono text-slate-600">{flag.entity_id}</span>
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
