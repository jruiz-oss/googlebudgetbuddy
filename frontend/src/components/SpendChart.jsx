/**
 * SpendChart — cumulative actual spend vs. expected linear trajectory for the current month.
 *
 * Props:
 *   monthlyBudget   – total monthly budget for this entity (campaign or account)
 *   history         – array of PacingData rows (need .date and .actual_spend)
 *                     (For the campaign view: only the rows that match the current month
 *                      will be plotted.)
 *   currentMtd      – optional override — current MTD actual spend. If omitted we use
 *                     the last history row.
 *   today           – optional override — Date object to anchor the X axis
 *                     (defaults to today). Used for testing.
 *
 * Renders a Chart.js line chart:
 *   • dashed gray line  = expected linear trajectory ($daily_target × day-of-month)
 *   • solid teal line   = actual cumulative spend
 *   • shaded gap        = (actual − expected), red when overspending, blue when underspending
 *   • horizontal line   = monthly budget cap
 *
 * Falls back to a friendly empty state when there's nothing to plot.
 */
import React, { useMemo, useRef } from 'react';
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Filler,
  Tooltip,
  Legend,
} from 'chart.js';
import { Line } from 'react-chartjs-2';
import { TrendingUp } from 'lucide-react';

ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Filler,
  Tooltip,
  Legend
);

function startOfMonth(d) {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}
function daysInMonthOf(d) {
  return new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
}

export default function SpendChart({
  monthlyBudget,
  history = [],
  currentMtd,
  today = new Date(),
  height = 240,
  title = 'Spend vs. target',
}) {
  const chartRef = useRef(null);

  const { data, options, hasData } = useMemo(() => {
    const monthStart = startOfMonth(today);
    const dim = daysInMonthOf(today);
    const dailyTarget = monthlyBudget > 0 ? monthlyBudget / dim : 0;

    // Build labels = day numbers 1..dim
    const labels = Array.from({ length: dim }, (_, i) => `${i + 1}`);

    // Expected linear: cumulative day-of-month × dailyTarget
    const expected = Array.from({ length: dim }, (_, i) => +(dailyTarget * (i + 1)).toFixed(2));

    // Filter history to current month and dedup by date (keep the highest-spend snapshot per day)
    const byDay = new Map();
    for (const row of history) {
      if (!row?.date || row.actual_spend == null) continue;
      const d = new Date(row.date + 'T00:00:00Z');
      if (d.getUTCFullYear() !== monthStart.getFullYear() || d.getUTCMonth() !== monthStart.getMonth()) continue;
      const day = d.getUTCDate();
      const prev = byDay.get(day);
      const candidate = Number(row.actual_spend) || 0;
      if (prev == null || candidate >= prev) byDay.set(day, candidate);
    }

    // If no history, but we have a current_mtd, plot a single point at today's day-of-month.
    if (byDay.size === 0 && currentMtd != null) {
      byDay.set(today.getDate(), Number(currentMtd) || 0);
    }

    // Build the actual line. Leave future days as null so Chart.js stops the line at "today".
    const actualSeries = Array.from({ length: dim }, () => null);
    for (const [day, val] of byDay.entries()) {
      if (day >= 1 && day <= dim) actualSeries[day - 1] = val;
    }

    const hasData = byDay.size > 0;

    // Today marker — vertical line via plugin annotation isn't loaded; we use a point.
    const todayDay = today.getDate();

    const data = {
      labels,
      datasets: [
        {
          label: 'Expected',
          data: expected,
          borderColor: 'oklch(75% 0.01 240)',
          backgroundColor: 'transparent',
          borderDash: [5, 4],
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0,
          order: 2,
        },
        {
          label: 'Actual',
          data: actualSeries,
          borderColor: 'oklch(58% 0.18 258)',
          backgroundColor: 'oklch(58% 0.18 258 / 0.08)',
          borderWidth: 2.5,
          pointRadius: (ctx) => (ctx.dataIndex + 1 === todayDay ? 4 : 0),
          pointBackgroundColor: 'oklch(58% 0.18 258)',
          pointBorderColor: '#fff',
          pointBorderWidth: 2,
          spanGaps: false,
          fill: {
            target: { value: 0 },
            above: 'oklch(58% 0.18 258 / 0.06)',
          },
          tension: 0.18,
          order: 1,
        },
      ],
    };

    const fmt$ = (n) => `$${(Number(n) || 0).toLocaleString('en-US', { maximumFractionDigits: 0 })}`;

    const options = {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          display: true,
          position: 'top',
          align: 'end',
          labels: {
            boxWidth: 10,
            boxHeight: 10,
            font: { family: 'Inter, system-ui, sans-serif', size: 11 },
            color: '#6b7280',
            usePointStyle: true,
          },
        },
        tooltip: {
          backgroundColor: 'rgba(15, 23, 42, 0.95)',
          padding: 10,
          titleFont: { family: 'Inter, system-ui, sans-serif', size: 12, weight: '600' },
          bodyFont: { family: 'Inter, system-ui, sans-serif', size: 12 },
          callbacks: {
            title: (items) => `Day ${items[0].label}`,
            label: (ctx) => {
              const v = ctx.parsed.y;
              if (v == null) return null;
              return `${ctx.dataset.label}: ${fmt$(v)}`;
            },
            afterBody: (items) => {
              const actual = items.find((i) => i.dataset.label === 'Actual')?.parsed?.y;
              const exp = items.find((i) => i.dataset.label === 'Expected')?.parsed?.y;
              if (actual == null || exp == null) return null;
              const gap = actual - exp;
              if (Math.abs(gap) < 1) return null;
              return gap > 0 ? `Over by ${fmt$(gap)}` : `Under by ${fmt$(Math.abs(gap))}`;
            },
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            color: 'oklch(60% 0.012 240)',
            font: { family: 'Geist, Inter, system-ui, sans-serif', size: 10 },
            maxRotation: 0,
            // Show ~10 ticks on the x axis to keep things readable.
            autoSkip: true,
            maxTicksLimit: 10,
          },
        },
        y: {
          beginAtZero: true,
          grid: { color: 'oklch(91% 0.006 240)' },
          ticks: {
            color: 'oklch(60% 0.012 240)',
            font: { family: 'Geist Mono, JetBrains Mono, ui-monospace, monospace', size: 10 },
            callback: (v) => fmt$(v),
          },
        },
      },
    };

    return { data, options, hasData };
  }, [monthlyBudget, history, currentMtd, today]);

  if (!monthlyBudget || monthlyBudget <= 0) {
    return (
      <div className="bb-chart-empty">
        <TrendingUp size={22} strokeWidth={1.5} aria-hidden="true" />
        <div className="bb-chart-empty-msg">Set a monthly budget to see the spend trajectory.</div>
      </div>
    );
  }

  return (
    <div>
      {title && (
        <div className="bb-chart-title">
          <TrendingUp size={14} aria-hidden="true" />
          {title}
        </div>
      )}
      <div style={{ height, position: 'relative' }}>
        <Line ref={chartRef} data={data} options={options} />
      </div>
      {!hasData && (
        <div className="bb-muted" style={{ fontSize: 12, marginTop: 8, textAlign: 'center' }}>
          No spend data yet for this month — run pacing to populate the actual line.
        </div>
      )}
    </div>
  );
}
