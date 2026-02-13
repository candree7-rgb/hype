'use client'

import { useState } from 'react'
import { format } from 'date-fns'
import StatsCards from '@/components/stats-cards'
import EquityChart from '@/components/equity-chart'
import TradesTable from '@/components/trades-table'
import TPDistributionChart from '@/components/tp-distribution'
import DCADistributionChart from '@/components/dca-distribution'
import TimeRangeSelector, { TimeRange } from '@/components/time-range-selector'
import DateRangePicker from '@/components/date-range-picker'
import EquitySimulator from '@/components/equity-simulator'

export default function Dashboard() {
  const [timeRange, setTimeRange] = useState<TimeRange>('1M')
  const [showDatePicker, setShowDatePicker] = useState(false)
  const [customDateRange, setCustomDateRange] = useState<{ from: string; to: string } | null>(null)

  const handleCustomDateApply = (from: string, to: string) => {
    setCustomDateRange({ from, to })
    setTimeRange('CUSTOM')
  }

  const customLabel = customDateRange
    ? `${format(new Date(customDateRange.from), 'MMM dd')} - ${format(new Date(customDateRange.to), 'MMM dd')}`
    : undefined

  return (
    <main className="min-h-screen bg-background">
      {/* Date Picker Modal */}
      <DateRangePicker
        isOpen={showDatePicker}
        onClose={() => setShowDatePicker(false)}
        onApply={handleCustomDateApply}
      />

      {/* Header */}
      <header className="border-b border-border bg-card/50 backdrop-blur-sm sticky top-0 z-40">
        <div className="container mx-auto px-4 py-4">
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
            <div>
              <h1 className="text-2xl font-bold text-foreground">
                *Systemic
              </h1>
              <p className="text-sm text-muted-foreground">
                Bybit Perpetual &bull; 20x Leverage &bull; Neo Cloud filter &bull; RZ DCA
              </p>
            </div>
            <TimeRangeSelector
              selected={timeRange}
              onSelect={setTimeRange}
              onCustomClick={() => setShowDatePicker(true)}
              customLabel={customLabel}
            />
          </div>
        </div>
      </header>

      {/* Content */}
      <div className="container mx-auto px-4 py-6 space-y-6">
        {/* Stats Cards */}
        <section>
          <StatsCards timeRange={timeRange} customDateRange={customDateRange} />
        </section>

        {/* Charts Row */}
        <section className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2">
            <EquityChart timeRange={timeRange} customDateRange={customDateRange} />
          </div>
          <div>
            <TPDistributionChart timeRange={timeRange} customDateRange={customDateRange} />
          </div>
        </section>

        {/* Equity Simulator */}
        <section>
          <EquitySimulator timeRange={timeRange} customDateRange={customDateRange} onChange={() => {}} />
        </section>

        {/* DCA Distribution */}
        <section>
          <DCADistributionChart timeRange={timeRange} customDateRange={customDateRange} />
        </section>

        {/* Trades Table */}
        <section>
          <TradesTable timeRange={timeRange} customDateRange={customDateRange} />
        </section>
      </div>

      {/* Footer */}
      <footer className="border-t border-border py-4 mt-8">
        <div className="container mx-auto px-4 text-center text-sm text-muted-foreground">
          *Systemic &bull; Auto-refreshes every 30s
        </div>
      </footer>
    </main>
  )
}
