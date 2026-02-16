'use client'

import { useEffect, useState, useMemo } from 'react'
import { Trade } from '@/lib/db'
import { formatCurrency, formatDate, formatDuration, cn } from '@/lib/utils'
import { TimeRange, TIME_RANGES } from './time-range-selector'
import { SimSettings, runSimulation } from '@/lib/simulation'

interface TradesTableProps {
  timeRange: TimeRange
  customDateRange?: { from: string; to: string } | null
  simSettings: SimSettings
}

type BadgeVariant = 'tp' | 'trail' | 'be' | 'sl' | 'neutral'

function getExitBadges(trade: Trade): { label: string; variant: BadgeVariant }[] {
  const reason = (trade.close_reason || '').toLowerCase()
  const badges: { label: string; variant: BadgeVariant }[] = []

  // Parse highest TP level from close_reason
  const tpMatch = reason.match(/tp(\d)/)
  const tpLevel = tpMatch ? parseInt(tpMatch[1]) : 0

  if (reason.includes('trail')) {
    // Trailing stop exit
    if (tpLevel >= 1) {
      badges.push({ label: `TP${tpLevel}`, variant: 'tp' })
    }
    badges.push({ label: 'TRAIL', variant: 'trail' })
  } else if (reason.includes('sl') || reason.includes('stop')) {
    // Stop loss exit
    if (tpLevel >= 1) {
      // TP was hit but SL triggered later = breakeven area
      badges.push({ label: `TP${tpLevel}`, variant: 'tp' })
      badges.push({ label: 'BE', variant: 'be' })
    } else if (trade.tp1_hit) {
      badges.push({ label: 'TP1', variant: 'tp' })
      badges.push({ label: 'BE', variant: 'be' })
    } else {
      badges.push({ label: 'SL', variant: 'sl' })
    }
  } else if (reason.includes('be')) {
    // BE-trail exit
    if (trade.tp1_hit) {
      badges.push({ label: 'TP1', variant: 'tp' })
    }
    badges.push({ label: 'BE', variant: 'be' })
  } else if (reason.includes('neo')) {
    // Neo cloud exit
    if (tpLevel >= 1) {
      badges.push({ label: `TP${tpLevel}`, variant: 'tp' })
    }
    badges.push({ label: 'Flip', variant: 'neutral' })
  } else if (reason.includes('manual') || reason.includes('tg')) {
    badges.push({ label: 'MANUAL', variant: 'neutral' })
  } else if (reason.includes('sync')) {
    badges.push({ label: 'SYNC', variant: 'neutral' })
  } else if (tpLevel >= 1) {
    // Generic TP exit
    badges.push({ label: `TP${tpLevel}`, variant: 'tp' })
  } else if (trade.tp1_hit) {
    badges.push({ label: 'TP1', variant: 'tp' })
  } else {
    badges.push({ label: reason.replace(/_/g, ' ').toUpperCase().slice(0, 8) || '-', variant: 'neutral' })
  }

  return badges
}

const badgeColors: Record<BadgeVariant, string> = {
  tp: 'bg-success/20 text-success',
  trail: 'bg-blue-500/20 text-blue-400',
  be: 'bg-warning/20 text-warning',
  sl: 'bg-danger/20 text-danger',
  neutral: 'bg-muted text-muted-foreground',
}

export default function TradesTable({ timeRange, customDateRange, simSettings }: TradesTableProps) {
  const [trades, setTrades] = useState<Trade[]>([])
  const [loading, setLoading] = useState(true)

  // Run simulation on current trades when simSettings are active
  const simResults = useMemo(() => {
    if (!simSettings || trades.length === 0) return null
    return runSimulation(trades, simSettings)
  }, [trades, simSettings])

  useEffect(() => {
    async function fetchTrades() {
      try {
        const params = new URLSearchParams({ limit: '50' })

        if (timeRange === 'CUSTOM' && customDateRange) {
          params.append('from', customDateRange.from)
          params.append('to', customDateRange.to)
        } else {
          const range = TIME_RANGES.find(r => r.value === timeRange)
          if (range?.days) params.append('days', range.days.toString())
        }

        const res = await fetch(`/api/trades?${params.toString()}`)
        if (!res.ok) {
          console.error('Trades API returned', res.status)
          setTrades([])
          return
        }
        const data = await res.json()
        setTrades(Array.isArray(data) ? data : [])
      } catch (error) {
        console.error('Failed to fetch trades:', error)
      } finally {
        setLoading(false)
      }
    }

    setLoading(true)
    fetchTrades()
    const interval = setInterval(fetchTrades, 30000)
    return () => clearInterval(interval)
  }, [timeRange, customDateRange])

  if (loading) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <div className="h-8 bg-muted rounded w-1/4 mb-4 animate-pulse"></div>
        <div className="space-y-2">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="h-16 bg-muted rounded animate-pulse"></div>
          ))}
        </div>
      </div>
    )
  }

  if (trades.length === 0) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <h2 className="text-xl font-bold mb-4">Trade History</h2>
        <div className="text-center text-muted-foreground py-8">
          No trades found for this period
        </div>
      </div>
    )
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
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">Symbol</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">Time</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">Side</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">Entry</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">Duration</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">P&L</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">P&L %</th>
              {simResults && (
                <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">Sim P&L</th>
              )}
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">Exit</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground uppercase">DCA</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {trades.map((trade) => (
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

                {/* Time */}
                <td className="px-4 py-4 text-sm text-muted-foreground">
                  {trade.closed_at ? formatDate(trade.closed_at) : '-'}
                </td>

                {/* Side */}
                <td className="px-4 py-4">
                  <span className={cn(
                    'px-2 py-1 rounded text-xs font-semibold',
                    trade.side === 'long'
                      ? 'bg-success/20 text-success'
                      : 'bg-danger/20 text-danger'
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
                  <span className={cn(
                    'font-semibold',
                    (trade.realized_pnl || 0) >= 0 ? 'text-success' : 'text-danger'
                  )}>
                    {(trade.realized_pnl || 0) >= 0 ? '+' : ''}
                    {formatCurrency(parseFloat(trade.realized_pnl?.toString() || '0'))}
                  </span>
                </td>

                {/* P&L % */}
                <td className="px-4 py-4">
                  <span className={cn(
                    'font-semibold text-sm',
                    (trade.pnl_pct_equity || 0) >= 0 ? 'text-success' : 'text-danger'
                  )}>
                    {(trade.pnl_pct_equity || 0) >= 0 ? '+' : ''}
                    {parseFloat(trade.pnl_pct_equity?.toString() || '0').toFixed(2)}%
                  </span>
                </td>

                {/* Sim P&L */}
                {simResults && (() => {
                  const sim = simResults.per_trade.get(trade.trade_id)
                  return (
                    <td className="px-4 py-4">
                      {sim ? (
                        <span className={cn(
                          'font-semibold',
                          sim.sim_pnl >= 0 ? 'text-success' : 'text-danger'
                        )}>
                          {sim.sim_pnl >= 0 ? '+' : ''}{formatCurrency(sim.sim_pnl)}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">-</span>
                      )}
                    </td>
                  )
                })()}

                {/* Exit badges */}
                <td className="px-4 py-4">
                  <div className="flex flex-wrap gap-1">
                    {getExitBadges(trade).map((badge, idx) => (
                      <span
                        key={idx}
                        className={cn(
                          'px-2 py-0.5 rounded text-xs font-semibold',
                          badgeColors[badge.variant]
                        )}
                      >
                        {badge.label}
                      </span>
                    ))}
                  </div>
                </td>

                {/* DCA badge */}
                <td className="px-4 py-4">
                  {trade.max_dca_reached > 0 ? (
                    <span className="px-2 py-0.5 rounded text-xs font-semibold bg-orange-500/20 text-orange-400">
                      DCA
                    </span>
                  ) : (
                    <span className="text-sm text-muted-foreground">-</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
