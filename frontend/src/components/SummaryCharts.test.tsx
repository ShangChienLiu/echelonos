import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import SummaryCharts from './SummaryCharts';
import type { SummaryData } from '../types';

describe('SummaryCharts', () => {
  const summary: SummaryData = {
    by_type: { Delivery: 3, Financial: 2 },
    by_status: { ACTIVE: 4, SUPERSEDED: 1 },
    by_responsible_party: { 'Very Long Responsible Party Name Corp': 5, Vendor: 3 },
  };

  it('renders all bar chart labels with a title attribute for truncated text', () => {
    render(<SummaryCharts summary={summary} />);

    const allLabels = [
      ...Object.keys(summary.by_type),
      ...Object.keys(summary.by_status),
      ...Object.keys(summary.by_responsible_party),
    ];

    for (const label of allLabels) {
      const el = screen.getByText(label);
      expect(el).toHaveAttribute('title', label);
    }
  });
});
