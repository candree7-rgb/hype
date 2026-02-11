'use client';

import { useEffect, useState } from 'react';
import { Stats } from '@/lib/db';
import { formatCurrency } from '@/lib/utils';

export default function StatsCards() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function fetchStats() {
      try {
        const res = await fetch('/api/stats');
        const data = await res.json();
        setStats(data);
      } catch (error) {
        console.error('Failed to fetch stats:', error);
      } finally {
        setLoading(false);
      }
    }

    fetchStats();
    const interval = setInterval(fetchStats, 30000);
    return () => clearInterval(interval);
  }, []);

  if (loading) {
    return (
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {[...Array(8)].map((_, i) => (
          <div key={i} className="bg-card border border-border rounded-lg p-4 md:p-6 animate-pulse">
            <div className="h-4 bg-muted rounded w-1/2 mb-2"></div>
            <div className="h-8 bg-muted rounded w-3/4"></div>
          </div>
        ))}
      </div>
    );
  }

  if (!stats || stats.total_trades === 0) {
    return (
      <div className="bg-card border border-border rounded-lg p-6 text-center">
        <p className="text-muted-foreground">No trade data available</p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
      <StatCard
        label="Total Trades"
        value={stats.total_trades.toString()}
        subValue={`${stats.wins}W / ${stats.breakeven}BE / ${stats.losses}L`}
      />
      <StatCard
        label="Win Rate"
        value={`${stats.win_rate.toFixed(1)}%`}
        variant={stats.win_rate >= 50 ? 'success' : 'danger'}
        subValue="Wins + Breakeven"
      />
      <StatCard
        label="Total Wins"
        value={stats.wins.toString()}
        valueColor="text-success"
      />
      <StatCard
        label="Total Losses"
        value={stats.losses.toString()}
        valueColor="text-danger"
        subValue="Pure SL, no TP"
      />
      <StatCard
        label="Total PnL"
        value={formatCurrency(stats.total_pnl)}
        variant={stats.total_pnl >= 0 ? 'success' : 'danger'}
        subValue={`${stats.total_pnl_pct >= 0 ? '+' : ''}${stats.total_pnl_pct.toFixed(2)}% Equity`}
      />
      <StatCard
        label="Avg PnL"
        value={`${stats.avg_pnl_pct >= 0 ? '+' : ''}${stats.avg_pnl_pct.toFixed(2)}%`}
        variant={stats.avg_pnl >= 0 ? 'success' : 'danger'}
        subValue={`${formatCurrency(stats.avg_pnl)} per trade`}
      />
      <StatCard
        label="Avg Win"
        value={`${stats.avg_win_pct >= 0 ? '+' : ''}${stats.avg_win_pct.toFixed(2)}%`}
        valueColor="text-success"
        subValue={`${formatCurrency(stats.avg_win)}`}
      />
      <StatCard
        label="Avg Loss"
        value={`${stats.avg_loss_pct.toFixed(2)}%`}
        valueColor="text-danger"
        subValue={`${formatCurrency(stats.avg_loss)}`}
      />
      <StatCard
        label="Win/Loss Ratio"
        value={`${stats.win_loss_ratio.toFixed(2)}:1`}
        variant={stats.win_loss_ratio >= 1 ? 'success' : 'danger'}
      />
      <StatCard
        label="Best Trade"
        value={formatCurrency(stats.best_trade)}
        valueColor="text-success"
      />
      <StatCard
        label="Worst Trade"
        value={formatCurrency(stats.worst_trade)}
        valueColor="text-danger"
      />
      <StatCard
        label="Avg DCAs Filled"
        value={stats.avg_dca_fills.toFixed(1)}
        subValue="per trade"
      />
      <StatCard
        label="Trailing Exits"
        value={stats.trailing_exits.toString()}
        subValue={`${((stats.trailing_exits / stats.total_trades) * 100).toFixed(0)}% of trades`}
      />
      <StatCard
        label="Stop Loss Exits"
        value={stats.sl_exits.toString()}
        valueColor="text-danger"
        subValue={`${((stats.sl_exits / stats.total_trades) * 100).toFixed(0)}% of trades`}
      />
      <StatCard
        label="BE Exits"
        value={stats.be_exits.toString()}
        subValue={`${((stats.be_exits / stats.total_trades) * 100).toFixed(0)}% of trades`}
      />
      <StatCard
        label="Breakeven"
        value={stats.breakeven.toString()}
        valueColor="text-muted-foreground"
        subValue="TP1+ but PnL <= 0"
      />
    </div>
  );
}

interface StatCardProps {
  label: string;
  value: string;
  subValue?: string;
  variant?: 'default' | 'success' | 'danger';
  valueColor?: string;
}

function StatCard({ label, value, subValue, variant = 'default', valueColor }: StatCardProps) {
  let borderClass = 'border-border';
  let textClass = valueColor || 'text-foreground';

  if (variant === 'success') {
    borderClass = 'border-success/20';
    textClass = 'text-success';
  } else if (variant === 'danger') {
    borderClass = 'border-danger/20';
    textClass = 'text-danger';
  }

  return (
    <div className={`bg-card border ${borderClass} rounded-lg p-4 md:p-6`}>
      <div className="text-sm text-muted-foreground mb-1">{label}</div>
      <div className={`text-2xl font-bold ${textClass}`}>{value}</div>
      {subValue && <div className="text-xs text-muted-foreground mt-1">{subValue}</div>}
    </div>
  );
}
