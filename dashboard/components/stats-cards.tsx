'use client'

import { useEffect, useState } from 'react'
import { Stats } from '@/lib/db'
import { formatCurrency } from '@/lib/utils'
import { TimeRange, TIME_RANGES } from './time-range-selector'

interface StatsCardsProps {
  timeRange: TimeRange
  customDateRange?: { from: string; to: string } | null
}

export default function StatsCards({ timeRange, customDateRange }: StatsCardsProps) {
  const [stats, setStats] = useState<Stats | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    async function fetchStats() {
      try {
        const params = new URLSearchParams()

        if (timeRange === 'CUSTOM' && customDateRange) {
          params.append('from', customDateRange.from)
          params.append('to', customDateRange.to)
        } else {
          const range = TIME_RANGES.find(r => r.value === timeRange)
          if (range?.days) params.append('days', range.days.toString())
        }

        const res = await fetch(`/api/stats?${params.toString()}`)
        if (!res.ok) {
          console.error('Stats API returned', res.status)
          setStats(null)
          return
        }
        const data = await res.json()
        setStats(data)
      } catch (error) {
        console.error('Failed to fetch stats:', error)
      } finally {
        setLoading(false)
      }
    }

    setLoading(true)
    fetchStats()
    const interval = setInterval(fetchStats, 30000)
    return () => clearInterval(interval)
  }, [timeRange, customDateRange])

  if (loading) {
    return (
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-4">
        {[...Array(10)].map((_, i) => (
          <div key={i} className="bg-card border border-border rounded-lg p-4 animate-pulse">
            <div className="h-4 bg-muted rounded w-1/2 mb-2"></div>
            <div className="h-8 bg-muted rounded w-3/4"></div>
          </div>
        ))}
      </div>
    )
  }

  if (!stats || stats.total_trades === 0) {
    return (
      <div className="bg-card border border-border rounded-lg p-6 text-center">
        <p className="text-muted-foreground">No trade data available for this period</p>
      </div>
    )
  }

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-4">
      <StatCard
        label="Total Trades"
        value={stats.total_trades.toString()}
        subValue={`${stats.wins}W / ${stats.breakeven}BE / ${stats.losses}L`}
      />
      <StatCard
        label="Win Rate"
        value={`${(stats.win_rate ?? 0).toFixed(1)}%`}
        variant={stats.win_rate >= 50 ? 'success' : 'danger'}
        subValue="Wins + Breakeven"
      />
      <StatCard
        label="Total PnL"
        value={formatCurrency(stats.total_pnl)}
        variant={stats.total_pnl >= 0 ? 'success' : 'danger'}
        subValue={`${(stats.total_pnl_pct ?? 0) >= 0 ? '+' : ''}${(stats.total_pnl_pct ?? 0).toFixed(2)}% Equity`}
      />
      <StatCard
        label="Profit Factor"
        value={stats.profit_factor === Infinity ? '∞' : (stats.profit_factor ?? 0).toFixed(2)}
        variant={stats.profit_factor >= 1.5 ? 'success' : stats.profit_factor >= 1 ? 'default' : 'danger'}
        subValue="Gross Win / Loss"
      />
      <StatCard
        label="Avg PnL"
        value={`${(stats.avg_pnl_pct ?? 0) >= 0 ? '+' : ''}${(stats.avg_pnl_pct ?? 0).toFixed(2)}%`}
        variant={stats.avg_pnl >= 0 ? 'success' : 'danger'}
        subValue={formatCurrency(stats.avg_pnl)}
      />
      <StatCard
        label="Avg Win"
        value={`+${(stats.avg_win_pct ?? 0).toFixed(2)}%`}
        valueColor="text-success"
        subValue={formatCurrency(stats.avg_win)}
      />
      <StatCard
        label="Avg Loss"
        value={`${(stats.avg_loss_pct ?? 0).toFixed(2)}%`}
        valueColor="text-danger"
        subValue={formatCurrency(stats.avg_loss)}
      />
      <StatCard
        label="Best Trade"
        value={formatCurrency(stats.best_trade)}
        valueColor="text-success"
      />
      <StatCard
        label="Worst Trade"
        value={formatCurrency(stats.worst_trade)}
        valueColor={stats.worst_trade >= 0 ? 'text-success' : 'text-danger'}
      />
      <StatCard
        label="TP Hit Rate"
        value={`${(stats.tp_rate ?? 0).toFixed(1)}%`}
        valueColor={stats.tp_rate > 50 ? 'text-success' : 'text-danger'}
        subValue="Take Profit exits"
      />
      <StatCard
        label="Stop Loss Rate"
        value={`${(stats.sl_rate ?? 0).toFixed(1)}%`}
        valueColor={stats.sl_rate < 50 ? 'text-success' : 'text-danger'}
        subValue="Stop Loss exits"
      />
      <StatCard
        label="Avg Duration"
        value={(stats.avg_duration ?? 0) > 60 ? `${((stats.avg_duration ?? 0) / 60).toFixed(1)}h` : `${(stats.avg_duration ?? 0).toFixed(0)}m`}
        subValue="Per trade"
      />
    </div>
  )
}

interface StatCardProps {
  label: string
  value: string
  subValue?: string
  variant?: 'default' | 'success' | 'danger'
  valueColor?: string
}

function StatCard({ label, value, subValue, variant = 'default', valueColor }: StatCardProps) {
  let borderClass = 'border-border'
  let textClass = valueColor || 'text-foreground'

  if (variant === 'success') {
    borderClass = 'border-success/30'
    textClass = valueColor || 'text-success'
  } else if (variant === 'danger') {
    borderClass = 'border-danger/30'
    textClass = valueColor || 'text-danger'
  }

  return (
    <div className={`bg-card border ${borderClass} rounded-lg p-4`}>
      <div className="text-sm text-muted-foreground mb-1">{label}</div>
      <div className={`text-2xl font-bold ${textClass}`}>{value}</div>
      {subValue && <div className="text-xs text-muted-foreground mt-1">{subValue}</div>}
    </div>
  )
}
