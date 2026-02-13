'use client'

import { useEffect, useState } from 'react'
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
        <h2 className="text-xl font-bold mb-4">TP Hit Rate</h2>
        <div className="h-64 flex items-center justify-center text-muted-foreground">
          No data available
        </div>
      </div>
    )
  }

  const colors: Record<string, string> = {
    'TP4': '#15803d',
    'TP3': '#16a34a',
    'TP2': '#22c55e',
    'TP1': '#4ade80',
    'Stop Loss': '#ef4444',
    'Other': '#f59e0b',
  }

  // Find max count for scaling bars
  const maxCount = Math.max(...data.map(d => d.count), 1)

  return (
    <div className="bg-card border border-border rounded-lg p-6">
      <h2 className="text-xl font-bold mb-2">TP Hit Rate</h2>
      <p className="text-sm text-muted-foreground mb-4">
        How many trades reached each level (cumulative)
      </p>

      <div className="flex flex-col gap-3">
        {data.map((d) => {
          const color = colors[d.level] || '#6b7280'
          const barWidth = Math.max((d.count / maxCount) * 100, 2)

          return (
            <div key={d.level} className="flex items-center gap-3">
              {/* Label */}
              <div className="w-20 flex items-center gap-2 shrink-0">
                <div
                  className="w-3 h-3 rounded-full shrink-0"
                  style={{ backgroundColor: color }}
                />
                <span className="text-sm font-medium">{d.level}</span>
              </div>

              {/* Bar */}
              <div className="flex-1 bg-muted rounded-full h-6 overflow-hidden">
                <div
                  className="h-full rounded-full transition-all duration-500"
                  style={{
                    width: `${barWidth}%`,
                    backgroundColor: color,
                  }}
                />
              </div>

              {/* Count + Percentage */}
              <div className="w-24 text-right shrink-0">
                <span className="text-sm font-bold" style={{ color }}>
                  {d.count}
                </span>
                <span className="text-sm text-muted-foreground ml-1">
                  ({d.percentage.toFixed(0)}%)
                </span>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
