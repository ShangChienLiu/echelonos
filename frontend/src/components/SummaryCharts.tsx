import clsx from 'clsx';
import type { SummaryData } from '../types';

interface SummaryChartsProps {
  summary: SummaryData;
}

const STATUS_BAR_COLORS: Record<string, string> = {
  ACTIVE: 'bg-emerald-500',
  SUPERSEDED: 'bg-amber-500',
  TERMINATED: 'bg-red-500',
  UNRESOLVED: 'bg-slate-400',
};

const TYPE_COLORS = [
  'bg-blue-500',
  'bg-indigo-500',
  'bg-violet-500',
  'bg-purple-500',
  'bg-fuchsia-500',
  'bg-pink-500',
  'bg-rose-500',
  'bg-teal-500',
  'bg-cyan-500',
  'bg-sky-500',
];

const PARTY_COLORS = [
  'bg-blue-600',
  'bg-emerald-600',
  'bg-violet-600',
  'bg-amber-600',
  'bg-rose-600',
];

function HorizontalBarChart({
  title,
  data,
  colorMap,
  colorList,
}: {
  title: string;
  data: Record<string, number>;
  colorMap?: Record<string, string>;
  colorList?: string[];
}) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const max = Math.max(...entries.map(([, v]) => v), 1);

  return (
    <div className="bg-white rounded-lg shadow-sm border border-slate-200 p-5">
      <h3 className="text-sm font-semibold text-slate-700 mb-4">{title}</h3>
      <div className="space-y-3">
        {entries.map(([label, count], idx) => {
          const barColor = colorMap?.[label] ?? colorList?.[idx % (colorList?.length ?? 1)] ?? 'bg-blue-500';
          const pct = (count / max) * 100;
          return (
            <div key={label}>
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-medium text-slate-600 truncate max-w-[160px]" title={label}>
                  {label}
                </span>
                <span className="text-xs font-semibold text-slate-800 ml-2">{count}</span>
              </div>
              <div className="w-full h-2 bg-slate-100 rounded-full overflow-hidden">
                <div
                  className={clsx('h-full rounded-full transition-all duration-500', barColor)}
                  style={{ width: `${pct}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function SummaryCharts({ summary }: SummaryChartsProps) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
      <HorizontalBarChart
        title="Obligations by Type"
        data={summary.by_type}
        colorList={TYPE_COLORS}
      />
      <HorizontalBarChart
        title="Obligations by Status"
        data={summary.by_status}
        colorMap={STATUS_BAR_COLORS}
      />
      <HorizontalBarChart
        title="Obligations by Responsible Party"
        data={summary.by_responsible_party}
        colorList={PARTY_COLORS}
      />
    </div>
  );
}
