'use client'

import { useEffect, useState } from 'react'
import { PieChart, Pie, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import { TimeRange, TIME_RANGES } from './time-range-selector'

interface TPDistributionProps {
  timeRange: TimeRange
  customDateRange?: { from: string; to: string } | null
}

interface ExitData {
  level: string
  count: number
  percentage: number
}

export default function TPDistributionChart({ timeRange, customDateRange }: TPDistributionProps) {
  const [data, setData] = useState<ExitData[]>([])
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

        const res = await fetch(`/api/tp-distribution?${params.toString()}`)
        if (!res.ok) {
          console.error('TP distribution API returned', res.status)
          setData([])
          return
        }
        const distribution = await res.json()
        setData(Array.isArray(distribution) ? distribution : [])
      } catch (error) {
        console.error('Failed to fetch exit distribution:', error)
      } finally {
        setLoading(false)
      }
    }

    setLoading(true)
    fetchData()
    const interval = setInterval(fetchData, 60000)
    return () => clearInterval(interval)
  }, [timeRange, customDateRange])

  if (loading) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <div className="h-8 bg-muted rounded w-1/3 mb-4 animate-pulse"></div>
        <div className="h-64 bg-muted rounded animate-pulse"></div>
      </div>
    )
  }

  if (data.length === 0) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <h2 className="text-xl font-bold mb-4">Exit Distribution</h2>
        <div className="h-64 flex items-center justify-center text-muted-foreground">
          No data available
        </div>
      </div>
    )
  }

  const colors: Record<string, string> = {
    'Take Profit': '#22c55e',
    'Stop Loss': '#ef4444',
    'Other': '#f59e0b',
  }

  const chartData = data.map(d => ({
    ...d,
    fill: colors[d.level] || '#6b7280',
  }))

  return (
    <div className="bg-card border border-border rounded-lg p-6">
      <h2 className="text-xl font-bold mb-2">Exit Distribution</h2>
      <p className="text-sm text-muted-foreground mb-4">
        How trades are exiting
      </p>

      <div className="flex flex-col md:flex-row gap-6">
        {/* Pie Chart */}
        <div className="flex-1">
          <ResponsiveContainer width="100%" height={200}>
            <PieChart>
              <Pie
                data={chartData}
                dataKey="count"
                nameKey="level"
                cx="50%"
                cy="50%"
                innerRadius={50}
                outerRadius={80}
                paddingAngle={2}
              >
                {chartData.map((entry, index) => (
                  <Cell key={`cell-${index}`} fill={entry.fill} />
                ))}
              </Pie>
              <Tooltip content={<CustomTooltip />} />
            </PieChart>
          </ResponsiveContainer>
        </div>

        {/* Legend */}
        <div className="flex-1 flex flex-col justify-center gap-3">
          {chartData.map((d) => (
            <div key={d.level} className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div
                  className="w-3 h-3 rounded-full"
                  style={{ backgroundColor: d.fill }}
                />
                <span className="text-sm">{d.level}</span>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-sm text-muted-foreground">{d.count} trades</span>
                <span className="text-sm font-bold" style={{ color: d.fill }}>
                  {d.percentage.toFixed(1)}%
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function CustomTooltip({ active, payload }: any) {
  if (!active || !payload || !payload[0]) return null
  const data = payload[0].payload
  return (
    <div className="bg-card border border-border rounded-lg p-3 shadow-lg">
      <p className="text-sm font-semibold mb-1">{data.level}</p>
      <p className="text-sm text-foreground">
        Count: <span className="font-bold">{data.count}</span>
      </p>
      <p className="text-sm text-muted-foreground">
        {data.percentage.toFixed(1)}% of all trades
      </p>
    </div>
  )
}
