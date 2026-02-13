import { NextResponse } from 'next/server'
import { getDCADistribution } from '@/lib/db'

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url)
    const daysParam = searchParams.get('days')
    const days = daysParam ? parseInt(daysParam) : undefined
    const from = searchParams.get('from') || undefined
    const to = searchParams.get('to') || undefined
    const distribution = await getDCADistribution(days, from, to)
    return NextResponse.json(distribution)
  } catch (error) {
    console.error('Failed to fetch DCA distribution:', error)
    return NextResponse.json({ error: 'Failed to fetch DCA distribution' }, { status: 500 })
  }
}

export const dynamic = 'force-dynamic'
export const revalidate = 0
