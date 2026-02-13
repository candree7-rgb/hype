'use client'

import { useEffect, useState } from 'react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import { DCADistribution } from '@/lib/db'
import { TimeRange, TIME_RANGES } from './time-range-selector'

interface DCADistributionProps {
  timeRange: TimeRange
  customDateRange?: { from: string; to: string } | null
}

export default function DCADistributionChart({ timeRange, customDateRange }: DCADistributionProps) {
  const [data, setData] = useState<DCADistribution[]>([])
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

        const res = await fetch(`/api/dca-distribution?${params.toString()}`)
        if (!res.ok) {
          console.error('DCA distribution API returned', res.status)
          setData([])
          return
        }
        const distribution = await res.json()
        setData(Array.isArray(distribution) ? distribution : [])
      } catch (error) {
        console.error('Failed to fetch DCA distribution:', error)
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
        <div className="h-48 bg-muted rounded animate-pulse"></div>
      </div>
    )
  }

  if (data.length === 0) {
    return (
      <div className="bg-card border border-border rounded-lg p-6">
        <h2 className="text-xl font-bold mb-4">DCA Distribution</h2>
        <div className="h-48 flex items-center justify-center text-muted-foreground">No data available</div>
      </div>
    )
  }

  const colors: Record<string, string> = {
    'NO DCA': '#22c55e',
    'DCA': '#f97316',
  }

  const chartData = data.map(d => ({
    ...d,
    fill: colors[d.label] || '#6b7280',
  }))

  const maxCount = Math.max(...chartData.map(d => d.count))

  return (
    <div className="bg-card border border-border rounded-lg p-6">
      <h2 className="text-xl font-bold mb-2">DCA Distribution</h2>
      <p className="text-sm text-muted-foreground mb-4">
        Trades with vs without DCA fills
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="#333" opacity={0.1} />
            <XAxis dataKey="label" stroke="#888" fontSize={12} tickLine={false} axisLine={false} />
            <YAxis stroke="#888" fontSize={12} tickLine={false} axisLine={false} domain={[0, maxCount]} />
            <Tooltip content={<CustomTooltip />} cursor={{ fill: 'rgba(255, 255, 255, 0.05)' }} />
            <Bar dataKey="count" radius={[8, 8, 0, 0]}>
              {chartData.map((entry, index) => (
                <Cell key={`cell-${index}`} fill={entry.fill} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>

        <div className="flex flex-col justify-center gap-4">
          {chartData.map((d) => (
            <div key={d.label} className="flex items-center justify-between p-3 bg-muted/30 rounded-lg">
              <div className="flex items-center gap-3">
                <div className="w-3 h-3 rounded-full" style={{ backgroundColor: d.fill }} />
                <span className="font-medium">{d.label}</span>
              </div>
              <div className="text-right">
                <div className="text-2xl font-bold" style={{ color: d.fill }}>{d.percentage.toFixed(1)}%</div>
                <div className="text-xs text-muted-foreground">{d.count} trades</div>
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
      <p className="text-sm font-semibold mb-1">{data.label}</p>
      <p className="text-sm text-foreground">Count: <span className="font-bold">{data.count}</span></p>
      <p className="text-sm text-muted-foreground">{data.percentage.toFixed(1)}% of all trades</p>
    </div>
  )
}
