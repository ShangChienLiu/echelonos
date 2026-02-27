import { Activity, CheckCircle, AlertTriangle, Flag } from 'lucide-react';

interface StatsCardsProps {
  total: number;
  active: number;
  superseded: number;
  flagCount: number;
}

export default function StatsCards({ total, active, superseded, flagCount }: StatsCardsProps) {
  const cards = [
    {
      label: 'Total Obligations',
      value: total,
      icon: Activity,
      borderColor: 'border-l-blue-500',
      iconColor: 'text-blue-500',
      bgColor: 'bg-blue-50',
    },
    {
      label: 'Active',
      value: active,
      icon: CheckCircle,
      borderColor: 'border-l-emerald-500',
      iconColor: 'text-emerald-500',
      bgColor: 'bg-emerald-50',
    },
    {
      label: 'Superseded',
      value: superseded,
      icon: AlertTriangle,
      borderColor: 'border-l-amber-500',
      iconColor: 'text-amber-500',
      bgColor: 'bg-amber-50',
    },
    {
      label: 'Flags',
      value: flagCount,
      icon: Flag,
      borderColor: 'border-l-red-500',
      iconColor: 'text-red-500',
      bgColor: 'bg-red-50',
    },
  ];

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      {cards.map((card) => {
        const Icon = card.icon;
        return (
          <div
            key={card.label}
            className={`bg-white rounded-lg shadow-sm border border-slate-200 border-l-4 ${card.borderColor} p-5 transition-shadow hover:shadow-md`}
          >
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-slate-500">{card.label}</p>
                <p className="text-3xl font-bold text-slate-900 mt-1">{card.value}</p>
              </div>
              <div className={`${card.bgColor} p-3 rounded-lg`}>
                <Icon className={`w-6 h-6 ${card.iconColor}`} />
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
