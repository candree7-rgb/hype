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
  per_trade: Map<string, SimTradeResult>
}

export function runSimulation(trades: Trade[], settings: SimSettings): SimSummary {
  // Sort chronologically (oldest first) for correct compounding order
  const sorted = [...trades].sort((a, b) =>
    new Date(a.closed_at).getTime() - new Date(b.closed_at).getTime()
  )

  let runningEquity = settings.equity
  let totalSimPnl = 0
  const perTrade = new Map<string, SimTradeResult>()

  for (const trade of sorted) {
    // With compounding: margin based on current equity
    // Without compounding: margin always based on initial equity
    const baseEquity = settings.compounding ? runningEquity : settings.equity
    const simMargin = baseEquity * (settings.tradePct / 100)

    // pnl_pct_margin = return on deployed margin (independent of equity size)
    const pnlPctMargin = parseFloat(trade.pnl_pct_margin?.toString() || '0')
    const simPnl = simMargin * (pnlPctMargin / 100)

    runningEquity += simPnl
    totalSimPnl += simPnl

    perTrade.set(trade.trade_id, {
      sim_pnl: simPnl,
      sim_equity_after: runningEquity,
    })
  }

  return {
    total_sim_pnl: totalSimPnl,
    final_equity: runningEquity,
    total_return_pct: settings.equity > 0 ? (totalSimPnl / settings.equity) * 100 : 0,
    per_trade: perTrade,
  }
}
