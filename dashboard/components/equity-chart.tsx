'use client'

import { useEffect, useState } from 'react'
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { DailyEquity } from '@/lib/db'
import { formatCurrency } from '@/lib/utils'
import { format } from 'date-fns'
import { TimeRange, TIME_RANGES } from './time-range-selector'

interface EquityChartProps {
  timeRange: TimeRange
  customDateRange?: { from: string; to: string } | null
}

export default function EquityChart({ timeRange, customDateRange }: EquityChartProps) {
  const [data, setData] = useState<DailyEquity[]>([])
  const [loading, setLoading] = useState(true)
  const [liveEquity, setLiveEquity] = useState<number | null>(null)

  useEffect(() => {
    async function fetchEquity() {
      try {
        const params = new URLSearchParams()

        if (timeRange === 'CUSTOM' && customDateRange) {
          params.append('from', customDateRange.from)
          params.append('to', customDateRange.to)
        } else {
          const range = TIME_RANGES.find(r => r.value === timeRange)
          if (range?.days) params.append('days', range.days.toString())
        }

        const res = await fetch(`/api/equity?${params.toString()}`)
        const equity = await res.json()
        setData(equity)

        // Fetch live equity from Bybit
        try {
          const liveRes = await fetch('/api/live-equity')
          const liveData = await liveRes.json()
          if (liveData.equity) setLiveEquity(liveData.equity)
        } catch (err) {
          // Live equity is optional
        }
      } catch (error) {
        console.error('Failed to fetch equity:', error)
      } finally {
        setLoading(false)
      }
    }

    setLoading(true)
    fetchEquity()
    const interval = setInterval(fetchEquity, 60000)
    return () => clearInterval(interval)
  }, [timeRange, customDateRange])

  if (loading) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <div className="h-8 bg-muted rounded w-1/4 mb-4 animate-pulse"></div>
        <div className="h-64 bg-muted rounded animate-pulse"></div>
      </div>
    )
  }

  if (data.length === 0) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <h2 className="text-xl font-bold mb-4">Equity Curve</h2>
        <div className="h-64 flex items-center justify-center text-muted-foreground">
          No equity data available
        </div>
      </div>
    )
  }

  const chartData = data.map(d => ({
    date: format(new Date(d.date), 'MMM dd'),
    fullDate: format(new Date(d.date), 'MMM dd, yyyy'),
    equity: parseFloat((d.equity || 0).toString()),
    pnl: parseFloat((d.daily_pnl || 0).toString()),
  }))

  const currentEquity = parseFloat((data[data.length - 1]?.equity || 0).toString())
  const startEquity = parseFloat((data[0]?.equity || 0).toString())
  const totalPnL = currentEquity - startEquity
  const totalPnLPct = startEquity > 0 ? ((totalPnL / startEquity) * 100) : 0
  const displayEquity = liveEquity !== null ? liveEquity : currentEquity

  return (
    <div className="bg-card border border-border rounded-lg p-6">
      <div className="flex flex-col gap-4 mb-6">
        <h2 className="text-xl font-bold">Equity Curve</h2>

        <div className="flex items-center justify-between p-4 bg-muted/50 rounded-lg">
          <div>
            <div className="text-sm text-muted-foreground mb-1">Current Equity</div>
            <div className="text-3xl font-bold text-foreground">
              {formatCurrency(displayEquity)}
            </div>
            {liveEquity !== null && (
              <div className="text-xs text-muted-foreground mt-1">Live from Bybit</div>
            )}
          </div>
          <div className="text-right">
            <div className="text-sm text-muted-foreground mb-1">
              {timeRange === 'CUSTOM' ? 'Custom' : timeRange} Performance
            </div>
            <div className={`text-2xl font-bold ${totalPnL >= 0 ? 'text-success' : 'text-danger'}`}>
              {totalPnL >= 0 ? '+' : ''}{formatCurrency(totalPnL)}
            </div>
            <div className={`text-sm ${totalPnL >= 0 ? 'text-success' : 'text-danger'}`}>
              {totalPnLPct >= 0 ? '+' : ''}{totalPnLPct.toFixed(2)}%
            </div>
          </div>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={300}>
        <AreaChart data={chartData}>
          <defs>
            <linearGradient id="colorEquity" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#22c55e" stopOpacity={0.3}/>
              <stop offset="95%" stopColor="#22c55e" stopOpacity={0}/>
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#333" opacity={0.1} />
          <XAxis dataKey="date" stroke="#888" fontSize={12} tickLine={false} axisLine={false} />
          <YAxis
            stroke="#888"
            fontSize={12}
            tickLine={false}
            axisLine={false}
            tickFormatter={(value) => `$${value.toFixed(0)}`}
            domain={['dataMin - 100', 'dataMax + 100']}
          />
          <Tooltip content={<CustomTooltip />} />
          <Area
            type="monotone"
            dataKey="equity"
            stroke="#22c55e"
            strokeWidth={2}
            fillOpacity={1}
            fill="url(#colorEquity)"
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

function CustomTooltip({ active, payload }: any) {
  if (!active || !payload || !payload[0]) return null
  const data = payload[0].payload
  return (
    <div className="bg-card border border-border rounded-lg p-3 shadow-lg">
      <p className="text-sm font-semibold mb-1">{data.fullDate}</p>
      <p className="text-sm text-foreground">
        Equity: <span className="font-bold">{formatCurrency(data.equity)}</span>
      </p>
      <p className={`text-sm ${data.pnl >= 0 ? 'text-success' : 'text-danger'}`}>
        Daily P&L: <span className="font-bold">{data.pnl >= 0 ? '+' : ''}{formatCurrency(data.pnl)}</span>
      </p>
    </div>
  )
}
