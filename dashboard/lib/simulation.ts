import { Trade } from './db'

export interface SimSettings {
  equity: number
  tradePct: number
  compounding: boolean
}

export interface SimTradeResult {
  sim_pnl: number
  sim_equity_after: number
}

export interface SimSummary {
  total_sim_pnl: number
  final_equity: number
  total_return_pct: number
  max_drawdown: number      // absolute $ drawdown from peak
  max_drawdown_pct: number  // drawdown as % of peak equity
  per_trade: Map<string, SimTradeResult>
}

// Bot's configured equity allocation per trade slot (matches config.py equity_per_trade_pct).
// pnl_pct_equity from the DB already bakes in this allocation.
const ORIGINAL_TRADE_PCT = Number(process.env.NEXT_PUBLIC_DEFAULT_TRADE_PCT) || 5

export function runSimulation(trades: Trade[], settings: SimSettings): SimSummary {
  // Sort chronologically (oldest first) for correct compounding order
  const sorted = [...trades].sort((a, b) =>
    new Date(a.closed_at).getTime() - new Date(b.closed_at).getTime()
  )

  // Scale PnL proportionally when user simulates a different equity % per trade.
  // pnl_pct_equity was recorded at ORIGINAL_TRADE_PCT (5%). If the user sets
  // tradePct=10%, returns double; tradePct=2.5%, returns halve.
  const scaleFactor = settings.tradePct / ORIGINAL_TRADE_PCT

  let runningEquity = settings.equity
  let totalSimPnl = 0
  let peakEquity = settings.equity
  let maxDrawdown = 0
  let maxDrawdownPct = 0
  const perTrade = new Map<string, SimTradeResult>()

  for (const trade of sorted) {
    const baseEquity = settings.compounding ? runningEquity : settings.equity

    // Use pnl_pct_equity (return on account equity) instead of pnl_pct_margin.
    // pnl_pct_margin is the return on DEPLOYED margin only, which is ~1/3 of
    // the slot allocation when DCA doesn't fill (DCA weights [1,2], E1=1/3).
    // Using pnl_pct_equity correctly accounts for the actual margin deployed.
    const pnlPctEquity = parseFloat(trade.pnl_pct_equity?.toString() || '0')
    const simPnl = baseEquity * (pnlPctEquity / 100) * scaleFactor

    runningEquity += simPnl
    totalSimPnl += simPnl

    // Track max drawdown (peak-to-trough)
    if (runningEquity > peakEquity) {
      peakEquity = runningEquity
    }
    const drawdown = peakEquity - runningEquity
    const drawdownPct = peakEquity > 0 ? (drawdown / peakEquity) * 100 : 0
    if (drawdown > maxDrawdown) {
      maxDrawdown = drawdown
      maxDrawdownPct = drawdownPct
    }

    perTrade.set(trade.trade_id, {
      sim_pnl: simPnl,
      sim_equity_after: runningEquity,
    })
  }

  return {
    total_sim_pnl: totalSimPnl,
    final_equity: runningEquity,
    total_return_pct: settings.equity > 0 ? (totalSimPnl / settings.equity) * 100 : 0,
    max_drawdown: maxDrawdown,
    max_drawdown_pct: maxDrawdownPct,
    per_trade: perTrade,
  }
}
