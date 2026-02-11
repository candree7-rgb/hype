'use client';

import StatsCards from '@/components/stats-cards';
import EquityChart from '@/components/equity-chart';
import TradesTable from '@/components/trades-table';
import TPDistributionChart from '@/components/tp-distribution';
import DCADistributionChart from '@/components/dca-distribution';

export default function Dashboard() {
  return (
    <main className="min-h-screen bg-background p-4 md:p-8">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-4xl font-bold mb-2">Signal DCA Bot v2</h1>
        <p className="text-muted-foreground">
          Telegram VIP Signals &bull; Bybit Perpetual Futures &bull; 20x Leverage &bull; Neo Cloud Filter
        </p>
      </div>

      {/* Stats Cards */}
      <div className="mb-8">
        <h2 className="text-2xl font-bold mb-4">All Time Performance</h2>
        <StatsCards />
      </div>

      {/* Equity Chart */}
      <div className="mb-8">
        <EquityChart />
      </div>

      {/* TP & DCA Distribution */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
        <TPDistributionChart />
        <DCADistributionChart />
      </div>

      {/* Trade History */}
      <div className="mb-8">
        <TradesTable />
      </div>

      {/* Footer */}
      <div className="text-center text-sm text-muted-foreground mt-12">
        <p>Signal DCA Bot v2 Dashboard</p>
      </div>
    </main>
  );
}
