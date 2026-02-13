'use client'

import { useState, useEffect, useMemo } from 'react'
import { Trade } from '@/lib/db'
import { SimSettings, runSimulation } from '@/lib/simulation'
import { formatCurrency } from '@/lib/utils'
import { TimeRange, TIME_RANGES } from './time-range-selector'

interface EquitySimulatorProps {
  timeRange: TimeRange
  customDateRange?: { from: string; to: string } | null
  onChange: (settings: SimSettings | null) => void
}

const STORAGE_KEY = 'equity-sim-v1'

export default function EquitySimulator({ timeRange, customDateRange, onChange }: EquitySimulatorProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [equity, setEquity] = useState(1000)
  const [tradePct, setTradePct] = useState(20)
  const [compounding, setCompounding] = useState(true)
  const [isActive, setIsActive] = useState(false)
  const [trades, setTrades] = useState<Trade[]>([])
  const [loaded, setLoaded] = useState(false)

  // Load persisted settings once
  useEffect(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY)
      if (saved) {
        const s = JSON.parse(saved)
        if (s.equity > 0) setEquity(s.equity)
        if (s.tradePct > 0) setTradePct(s.tradePct)
        if (typeof s.compounding === 'boolean') setCompounding(s.compounding)
        if (s.isActive) {
          setIsActive(true)
          setIsOpen(true)
        }
      }
    } catch { /* ignore */ }
    setLoaded(true)
  }, [])

  // Persist settings
  useEffect(() => {
    if (!loaded) return
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ equity, tradePct, compounding, isActive }))
  }, [equity, tradePct, compounding, isActive, loaded])

  // Propagate settings to parent
  useEffect(() => {
    if (!loaded) return
    onChange(isActive ? { equity, tradePct, compounding } : null)
  }, [equity, tradePct, compounding, isActive, loaded]) // intentionally exclude onChange to prevent loops

  // Fetch trades for summary calculation
  useEffect(() => {
    if (!isActive) {
      setTrades([])
      return
    }

    async function fetchTrades() {
      try {
        const params = new URLSearchParams({ limit: '500' })
        if (timeRange === 'CUSTOM' && customDateRange) {
          params.append('from', customDateRange.from)
          params.append('to', customDateRange.to)
        } else {
          const range = TIME_RANGES.find(r => r.value === timeRange)
          if (range?.days) params.append('days', range.days.toString())
        }
        const res = await fetch(`/api/trades?${params.toString()}`)
        if (!res.ok) return
        const data = await res.json()
        setTrades(Array.isArray(data) ? data : [])
      } catch { /* ignore */ }
    }

    fetchTrades()
    const interval = setInterval(fetchTrades, 30000)
    return () => clearInterval(interval)
  }, [isActive, timeRange, customDateRange])

  // Run simulation
  const summary = useMemo(() => {
    if (!isActive || trades.length === 0) return null
    return runSimulation(trades, { equity, tradePct, compounding })
  }, [trades, equity, tradePct, compounding, isActive])

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden">
      {/* Header - always visible */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full px-4 py-3 flex items-center justify-between hover:bg-muted/30 transition-colors"
      >
        <div className="flex items-center gap-3">
          <svg className="w-5 h-5 text-primary" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M9 7h6m0 10v-3m-3 3h.01M9 17h.01M9 14h.01M12 14h.01M15 11h.01M12 11h.01M9 11h.01M7 21h10a2 2 0 002-2V5a2 2 0 00-2-2H7a2 2 0 00-2 2v14a2 2 0 002 2z" />
          </svg>
          <div className="text-left flex items-center gap-2">
            <span className="font-semibold text-sm">Equity Simulator</span>
            {isActive && (
              <span className="px-2 py-0.5 rounded text-xs font-semibold bg-primary/20 text-primary">
                ACTIVE
              </span>
            )}
            {isActive && summary && (
              <span className={`text-xs font-semibold ${summary.total_sim_pnl >= 0 ? 'text-success' : 'text-danger'}`}>
                {summary.total_sim_pnl >= 0 ? '+' : ''}{formatCurrency(summary.total_sim_pnl)}
                {' '}({summary.total_return_pct >= 0 ? '+' : ''}{summary.total_return_pct.toFixed(1)}%)
              </span>
            )}
          </div>
        </div>
        <svg
          className={`w-5 h-5 text-muted-foreground transition-transform ${isOpen ? 'rotate-180' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {/* Collapsible content */}
      {isOpen && (
        <div className="px-4 pb-4 border-t border-border pt-4 space-y-4">
          {/* Controls row */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {/* Equity */}
            <div>
              <label className="text-sm text-muted-foreground mb-1.5 block">Start Equity ($)</label>
              <div className="flex items-center gap-3">
                <input
                  type="number"
                  min={100}
                  max={1000000}
                  step={100}
                  value={equity}
                  onChange={(e) => setEquity(Math.max(100, Number(e.target.value) || 100))}
                  className="w-28 bg-muted border border-border rounded px-3 py-1.5 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-primary"
                />
                <input
                  type="range"
                  min={100}
                  max={50000}
                  step={100}
                  value={Math.min(equity, 50000)}
                  onChange={(e) => setEquity(Number(e.target.value))}
                  className="flex-1 h-1.5"
                  style={{ accentColor: 'hsl(var(--primary))' }}
                />
              </div>
            </div>

            {/* Trade Size % */}
            <div>
              <label className="text-sm text-muted-foreground mb-1.5 block">Trade Size (% of Equity)</label>
              <div className="flex items-center gap-3">
                <div className="flex items-center gap-1">
                  <input
                    type="number"
                    min={1}
                    max={100}
                    value={tradePct}
                    onChange={(e) => setTradePct(Math.min(100, Math.max(1, Number(e.target.value) || 1)))}
                    className="w-16 bg-muted border border-border rounded px-3 py-1.5 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-primary"
                  />
                  <span className="text-sm text-muted-foreground">%</span>
                </div>
                <input
                  type="range"
                  min={1}
                  max={100}
                  step={1}
                  value={tradePct}
                  onChange={(e) => setTradePct(Number(e.target.value))}
                  className="flex-1 h-1.5"
                  style={{ accentColor: 'hsl(var(--primary))' }}
                />
              </div>
            </div>
          </div>

          {/* Compounding toggle + Activate button */}
          <div className="flex items-center justify-between">
            <button
              type="button"
              onClick={() => setCompounding(!compounding)}
              className="flex items-center gap-3 cursor-pointer"
            >
              <div className={`relative w-11 h-6 rounded-full transition-colors ${
                compounding ? 'bg-primary' : 'bg-muted-foreground/30'
              }`}>
                <div className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform ${
                  compounding ? 'translate-x-5' : ''
                }`} />
              </div>
              <span className="text-sm text-muted-foreground">
                Compounding {compounding ? 'ON' : 'OFF'}
              </span>
            </button>

            <button
              onClick={() => setIsActive(!isActive)}
              className={`px-4 py-2 rounded-lg text-sm font-semibold transition-colors ${
                isActive
                  ? 'bg-danger/20 text-danger hover:bg-danger/30'
                  : 'bg-primary/20 text-primary hover:bg-primary/30'
              }`}
            >
              {isActive ? 'Deactivate' : 'Activate'}
            </button>
          </div>

          {/* Simulation Results Summary */}
          {isActive && summary && (
            <div className="grid grid-cols-3 gap-3 pt-2 border-t border-border">
              <div className="bg-muted/50 rounded-lg p-3 text-center">
                <div className="text-xs text-muted-foreground mb-1">Start</div>
                <div className="text-sm font-bold font-mono">{formatCurrency(equity)}</div>
              </div>
              <div className="bg-muted/50 rounded-lg p-3 text-center">
                <div className="text-xs text-muted-foreground mb-1">Sim P&L</div>
                <div className={`text-sm font-bold font-mono ${summary.total_sim_pnl >= 0 ? 'text-success' : 'text-danger'}`}>
                  {summary.total_sim_pnl >= 0 ? '+' : ''}{formatCurrency(summary.total_sim_pnl)}
                </div>
                <div className={`text-xs ${summary.total_return_pct >= 0 ? 'text-success' : 'text-danger'}`}>
                  {summary.total_return_pct >= 0 ? '+' : ''}{summary.total_return_pct.toFixed(2)}%
                </div>
              </div>
              <div className="bg-muted/50 rounded-lg p-3 text-center">
                <div className="text-xs text-muted-foreground mb-1">Final Equity</div>
                <div className={`text-sm font-bold font-mono ${
                  summary.final_equity >= equity ? 'text-success' : 'text-danger'
                }`}>
                  {formatCurrency(summary.final_equity)}
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
