'use client'

import { useEffect, useState, useMemo } from 'react'
import { Stats, Trade } from '@/lib/db'
import { formatCurrency } from '@/lib/utils'
import { TimeRange, TIME_RANGES } from './time-range-selector'
import { SimSettings, runSimulation } from '@/lib/simulation'

interface StatsCardsProps {
  timeRange: TimeRange
  customDateRange?: { from: string; to: string } | null
  simSettings: SimSettings
}

export default function StatsCards({ timeRange, customDateRange, simSettings }: StatsCardsProps) {
  const [stats, setStats] = useState<Stats | null>(null)
  const [trades, setTrades] = useState<Trade[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    async function fetchData() {
      try {
        const params = new URLSearchParams()

        if (timeRange === 'CUSTOM' && customDateRange) {
          params.append('from', customDateRange.from)
          params.append('to', customDateRange.to)
        } else {
          const range = TIME_RANGES.find(r => r.value === timeRange)
          if (range?.days) params.append('days', range.days.toString())
        }

        const tradeParams = new URLSearchParams(params)
        tradeParams.set('limit', '500')

        const [statsRes, tradesRes] = await Promise.all([
          fetch(`/api/stats?${params.toString()}`),
          fetch(`/api/trades?${tradeParams.toString()}`),
        ])

        if (statsRes.ok) setStats(await statsRes.json())
        else setStats(null)

        if (tradesRes.ok) {
          const data = await tradesRes.json()
          setTrades(Array.isArray(data) ? data : [])
        }
      } catch (error) {
        console.error('Failed to fetch data:', error)
      } finally {
        setLoading(false)
      }
    }

    setLoading(true)
    fetchData()
    const interval = setInterval(fetchData, 30000)
    return () => clearInterval(interval)
  }, [timeRange, customDateRange])

  // Run simulation and compute sim stats
  const simStats = useMemo(() => {
    if (trades.length === 0) return null
    const sim = runSimulation(trades, simSettings)
    const perTrade = Array.from(sim.per_trade.values())
    const wins = perTrade.filter(t => t.sim_pnl > 0)
    const losses = perTrade.filter(t => t.sim_pnl < 0)
    const grossProfit = wins.reduce((s, t) => s + t.sim_pnl, 0)
    const grossLoss = Math.abs(losses.reduce((s, t) => s + t.sim_pnl, 0))

    return {
      total_pnl: sim.total_sim_pnl,
      total_pnl_pct: sim.total_return_pct,
      avg_pnl: sim.total_sim_pnl / perTrade.length,
      avg_pnl_pct: (sim.total_sim_pnl / perTrade.length) / simSettings.equity * 100,
      avg_win: wins.length > 0 ? grossProfit / wins.length : 0,
      avg_win_pct: wins.length > 0 ? (grossProfit / wins.length) / simSettings.equity * 100 : 0,
      avg_loss: losses.length > 0 ? -(grossLoss / losses.length) : 0,
      avg_loss_pct: losses.length > 0 ? -(grossLoss / losses.length) / simSettings.equity * 100 : 0,
      best_trade: perTrade.length > 0 ? Math.max(...perTrade.map(t => t.sim_pnl)) : 0,
      worst_trade: perTrade.length > 0 ? Math.min(...perTrade.map(t => t.sim_pnl)) : 0,
      profit_factor: grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? Infinity : 0,
      max_drawdown: sim.max_drawdown,
      max_drawdown_pct: sim.max_drawdown_pct,
    }
  }, [trades, simSettings])

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

  // Use sim values for monetary stats, keep DB values for counts/rates
  const s = simStats
  const totalPnl = s ? s.total_pnl : stats.total_pnl
  const totalPnlPct = s ? s.total_pnl_pct : stats.total_pnl_pct
  const avgPnl = s ? s.avg_pnl : stats.avg_pnl
  const avgPnlPct = s ? s.avg_pnl_pct : stats.avg_pnl_pct
  const avgWin = s ? s.avg_win : stats.avg_win
  const avgWinPct = s ? s.avg_win_pct : stats.avg_win_pct
  const avgLoss = s ? s.avg_loss : stats.avg_loss
  const avgLossPct = s ? s.avg_loss_pct : stats.avg_loss_pct
  const bestTrade = s ? s.best_trade : stats.best_trade
  const worstTrade = s ? s.worst_trade : stats.worst_trade
  const profitFactor = s ? s.profit_factor : stats.profit_factor
  const maxDrawdown = s?.max_drawdown ?? 0
  const maxDrawdownPct = s?.max_drawdown_pct ?? 0

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
        value={formatCurrency(totalPnl)}
        variant={totalPnl >= 0 ? 'success' : 'danger'}
        subValue={`${totalPnlPct >= 0 ? '+' : ''}${totalPnlPct.toFixed(2)}% Equity`}
      />
      <StatCard
        label="Profit Factor"
        value={profitFactor === Infinity ? '∞' : profitFactor.toFixed(2)}
        variant={profitFactor >= 1.5 ? 'success' : profitFactor >= 1 ? 'default' : 'danger'}
        subValue="Gross Win / Loss"
      />
      <StatCard
        label="Avg PnL"
        value={`${avgPnlPct >= 0 ? '+' : ''}${avgPnlPct.toFixed(2)}%`}
        variant={avgPnl >= 0 ? 'success' : 'danger'}
        subValue={formatCurrency(avgPnl)}
      />
      <StatCard
        label="Avg Win"
        value={`+${avgWinPct.toFixed(2)}%`}
        valueColor="text-success"
        subValue={formatCurrency(avgWin)}
      />
      <StatCard
        label="Avg Loss"
        value={`${avgLossPct.toFixed(2)}%`}
        valueColor="text-danger"
        subValue={formatCurrency(avgLoss)}
      />
      <StatCard
        label="Best Trade"
        value={formatCurrency(bestTrade)}
        valueColor="text-success"
      />
      <StatCard
        label="Worst Trade"
        value={formatCurrency(worstTrade)}
        valueColor={worstTrade >= 0 ? 'text-success' : 'text-danger'}
      />
      <StatCard
        label="Stop Loss Rate"
        value={`${(stats.sl_rate ?? 0).toFixed(1)}%`}
        valueColor={stats.sl_rate < 50 ? 'text-success' : 'text-danger'}
        subValue="Stop Loss exits"
      />
      <StatCard
        label="Max Drawdown"
        value={`-${maxDrawdownPct.toFixed(2)}%`}
        variant={maxDrawdownPct > 10 ? 'danger' : 'default'}
        valueColor="text-danger"
        subValue={`${formatCurrency(-maxDrawdown)} from peak`}
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
