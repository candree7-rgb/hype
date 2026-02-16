'use client'

import { useState, useEffect } from 'react'
import { SimSettings } from '@/lib/simulation'

interface EquitySimulatorProps {
  onChange: (settings: SimSettings) => void
  isSimulated?: boolean
}

const STORAGE_KEY = 'equity-sim-v1'

export default function EquitySimulator({ onChange, isSimulated = true }: EquitySimulatorProps) {
  const [equity, setEquity] = useState(10000)
  const [tradePct, setTradePct] = useState(5)
  const [compounding, setCompounding] = useState(true)
  const [loaded, setLoaded] = useState(false)

  // Load persisted settings once (only in simulated mode)
  useEffect(() => {
    if (isSimulated) {
      try {
        const saved = localStorage.getItem(STORAGE_KEY)
        if (saved) {
          const s = JSON.parse(saved)
          if (s.equity > 0) setEquity(s.equity)
          if (s.tradePct > 0) setTradePct(s.tradePct)
          if (typeof s.compounding === 'boolean') setCompounding(s.compounding)
        }
      } catch { /* ignore */ }
    }
    setLoaded(true)
  }, [isSimulated])

  // In real mode: fetch actual account equity from Bybit
  useEffect(() => {
    if (isSimulated) return
    async function fetchRealEquity() {
      try {
        const res = await fetch('/api/live-equity')
        if (!res.ok) return
        const data = await res.json()
        if (data.equity > 0) {
          setEquity(Math.round(data.equity))
          setTradePct(5)
          setCompounding(true)
        }
      } catch { /* ignore */ }
    }
    fetchRealEquity()
  }, [isSimulated])

  // Persist and propagate settings
  useEffect(() => {
    if (!loaded) return
    if (isSimulated) {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ equity, tradePct, compounding }))
    }
    onChange({ equity, tradePct, compounding })
  }, [equity, tradePct, compounding, loaded, isSimulated])

  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
      {/* Equity */}
      <div className="flex items-center gap-2">
        <label className="text-sm text-muted-foreground whitespace-nowrap">Equity</label>
        <span className="text-sm text-muted-foreground">$</span>
        <input
          type="number"
          min={100}
          max={1000000}
          step={100}
          value={equity}
          onChange={(e) => setEquity(Math.max(100, Number(e.target.value) || 100))}
          className="w-24 bg-muted border border-border rounded px-2 py-1 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-primary"
        />
        <input
          type="range"
          min={100}
          max={50000}
          step={100}
          value={Math.min(equity, 50000)}
          onChange={(e) => setEquity(Number(e.target.value))}
          className="w-24 h-1.5"
          style={{ accentColor: 'hsl(var(--primary))' }}
        />
      </div>

      {/* Trade Size */}
      <div className="flex items-center gap-2">
        <label className="text-sm text-muted-foreground whitespace-nowrap">Trade</label>
        <div className="flex items-center gap-1">
          <input
            type="number"
            min={1}
            max={100}
            value={tradePct}
            onChange={(e) => setTradePct(Math.min(100, Math.max(1, Number(e.target.value) || 1)))}
            className="w-14 bg-muted border border-border rounded px-2 py-1 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-primary"
          />
          <span className="text-sm text-muted-foreground">%</span>
        </div>
        <input
          type="range"
          min={1}
          max={50}
          step={1}
          value={Math.min(tradePct, 50)}
          onChange={(e) => setTradePct(Number(e.target.value))}
          className="w-20 h-1.5"
          style={{ accentColor: 'hsl(var(--primary))' }}
        />
      </div>

      {/* Compounding toggle */}
      <button
        type="button"
        onClick={() => setCompounding(!compounding)}
        className="flex items-center gap-2 cursor-pointer"
      >
        <div className={`relative w-9 h-5 rounded-full transition-colors ${
          compounding ? 'bg-primary' : 'bg-muted-foreground/30'
        }`}>
          <div className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform ${
            compounding ? 'translate-x-4' : ''
          }`} />
        </div>
        <span className="text-sm text-muted-foreground">
          Compound
        </span>
      </button>
    </div>
  )
}
