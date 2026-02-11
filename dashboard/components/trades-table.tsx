'use client';

import { useEffect, useState } from 'react';
import { Trade } from '@/lib/db';
import { formatCurrency, formatDate, formatDuration, cn } from '@/lib/utils';

function formatExitReason(closeReason: string): { label: string; variant: 'tp' | 'trail' | 'sl' | 'neutral' }[] {
  if (!closeReason) return [{ label: '-', variant: 'neutral' }];

  const reason = closeReason.toLowerCase();
  const badges: { label: string; variant: 'tp' | 'trail' | 'sl' | 'neutral' }[] = [];

  if (reason.includes('tp1')) badges.push({ label: 'TP1', variant: 'tp' });
  if (reason.includes('tp2')) badges.push({ label: 'TP2', variant: 'tp' });
  if (reason.includes('tp3')) badges.push({ label: 'TP3', variant: 'tp' });
  if (reason.includes('tp4')) badges.push({ label: 'TP4', variant: 'tp' });
  if (reason.includes('trail')) badges.push({ label: 'TRAIL', variant: 'trail' });
  if (reason.includes('be')) badges.push({ label: 'BE', variant: 'neutral' });
  if (reason.includes('hard sl') || reason.includes('stop') || reason.includes('safety sl')) {
    badges.push({ label: 'SL', variant: 'sl' });
  }
  if (reason.includes('neo')) badges.push({ label: 'NEO', variant: 'neutral' });
  if (reason.includes('opposite')) badges.push({ label: 'FLIP', variant: 'neutral' });

  if (badges.length === 0) {
    badges.push({ label: reason.replace(/_/g, ' ').toUpperCase().slice(0, 8), variant: 'neutral' });
  }

  return badges;
}

export default function TradesTable() {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [loading, setLoading] = useState(true);
  const [sortField, setSortField] = useState<keyof Trade>('closed_at');
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('desc');

  useEffect(() => {
    async function fetchTrades() {
      try {
        const res = await fetch('/api/trades?limit=50');
        const data = await res.json();
        setTrades(data);
      } catch (error) {
        console.error('Failed to fetch trades:', error);
      } finally {
        setLoading(false);
      }
    }

    fetchTrades();
    const interval = setInterval(fetchTrades, 30000);
    return () => clearInterval(interval);
  }, []);

  const handleSort = (field: keyof Trade) => {
    if (sortField === field) {
      setSortDirection(sortDirection === 'asc' ? 'desc' : 'asc');
    } else {
      setSortField(field);
      setSortDirection('desc');
    }
  };

  const sortedTrades = [...trades].sort((a, b) => {
    const aVal = a[sortField];
    const bVal = b[sortField];
    if (aVal === null || aVal === undefined) return 1;
    if (bVal === null || bVal === undefined) return -1;
    if (aVal < bVal) return sortDirection === 'asc' ? -1 : 1;
    if (aVal > bVal) return sortDirection === 'asc' ? 1 : -1;
    return 0;
  });

  if (loading) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <div className="h-8 bg-muted rounded w-1/4 mb-4"></div>
        <div className="space-y-2">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="h-16 bg-muted rounded animate-pulse"></div>
          ))}
        </div>
      </div>
    );
  }

  if (trades.length === 0) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <h2 className="text-xl font-bold mb-4">Trade History</h2>
        <div className="text-center text-muted-foreground py-8">No trades found</div>
      </div>
    );
  }

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden">
      <div className="p-6 pb-4">
        <h2 className="text-xl font-bold">Trade History</h2>
        <p className="text-sm text-muted-foreground mt-1">Last {trades.length} trades</p>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full">
          <thead className="border-y border-border bg-muted/30">
            <tr>
              <TH onClick={() => handleSort('symbol')}>Symbol</TH>
              <TH onClick={() => handleSort('closed_at')}>Close Time</TH>
              <TH onClick={() => handleSort('side')}>Position</TH>
              <TH onClick={() => handleSort('entry_price')}>Entry</TH>
              <TH onClick={() => handleSort('duration_minutes')}>Duration</TH>
              <TH onClick={() => handleSort('realized_pnl')}>P&L</TH>
              <TH onClick={() => handleSort('pnl_pct_equity')}>P&L %</TH>
              <TH onClick={() => handleSort('close_reason')}>Exit</TH>
              <TH>DCAs</TH>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {sortedTrades.map((trade) => (
              <tr key={trade.trade_id} className="hover:bg-muted/20 transition-colors">
                {/* Symbol */}
                <td className="px-4 py-4">
                  <div className="flex items-center gap-2">
                    <span className="font-mono font-semibold">
                      {trade.symbol.replace('USDT', '')}
                    </span>
                    <span className="px-1.5 py-0.5 rounded text-xs font-semibold bg-muted/80 text-muted-foreground">
                      {trade.leverage}x
                    </span>
                  </div>
                </td>

                {/* Close Time */}
                <td className="px-4 py-4 text-sm text-muted-foreground">
                  {trade.closed_at ? formatDate(trade.closed_at) : '-'}
                </td>

                {/* Position */}
                <td className="px-4 py-4">
                  <span className={cn(
                    'px-2 py-1 rounded text-xs font-semibold',
                    trade.side === 'long' ? 'bg-success/20 text-success' : 'bg-danger/20 text-danger'
                  )}>
                    {trade.side.toUpperCase()}
                  </span>
                </td>

                {/* Entry */}
                <td className="px-4 py-4 font-mono text-sm">
                  ${parseFloat(trade.entry_price?.toString() || '0').toFixed(4)}
                </td>

                {/* Duration */}
                <td className="px-4 py-4 text-sm text-muted-foreground">
                  {formatDuration(trade.duration_minutes)}
                </td>

                {/* P&L */}
                <td className="px-4 py-4">
                  <span className={cn('font-semibold', trade.realized_pnl >= 0 ? 'text-success' : 'text-danger')}>
                    {trade.realized_pnl >= 0 ? '+' : ''}
                    {formatCurrency(parseFloat(trade.realized_pnl?.toString() || '0'))}
                  </span>
                </td>

                {/* P&L % */}
                <td className="px-4 py-4">
                  <span className={cn('font-semibold text-sm', trade.pnl_pct_equity >= 0 ? 'text-success' : 'text-danger')}>
                    {trade.pnl_pct_equity >= 0 ? '+' : ''}
                    {parseFloat(trade.pnl_pct_equity?.toString() || '0').toFixed(2)}%
                  </span>
                </td>

                {/* Exit */}
                <td className="px-4 py-4">
                  <div className="flex flex-wrap gap-1">
                    {formatExitReason(trade.close_reason).map((badge, idx) => (
                      <span
                        key={idx}
                        className={cn(
                          'px-2 py-0.5 rounded text-xs font-semibold',
                          badge.variant === 'tp' && 'bg-success/20 text-success',
                          badge.variant === 'trail' && 'bg-primary/20 text-primary',
                          badge.variant === 'sl' && 'bg-danger/20 text-danger',
                          badge.variant === 'neutral' && 'bg-muted text-muted-foreground'
                        )}
                      >
                        {badge.label}
                      </span>
                    ))}
                  </div>
                </td>

                {/* DCAs */}
                <td className="px-4 py-4 text-sm text-muted-foreground">
                  {trade.max_dca_reached}/2
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function TH({ children, onClick }: { children: React.ReactNode; onClick?: () => void }) {
  return (
    <th
      onClick={onClick}
      className={cn(
        'px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase tracking-wider',
        onClick && 'cursor-pointer hover:text-foreground transition-colors'
      )}
    >
      {children}
    </th>
  );
}
