export interface Post {
  id: string; symbol: string; platform: string; text: string;
  timestamp: string; sentiment: string;
  scores: Record<string, number>; topic_label: string | null;
  engagement: number;
}
export interface SentimentStat { sentiment: string; count: number; }
export interface SourceHealth {
  platform: string;
  posts_1h: number;
  posts_24h: number;
  last_ingest: string | null;
  age_seconds: number | null;
  baseline_per_hour: number | null;
  status: "active" | "quiet" | "stalled" | "silent";
}
export interface TopicStat { topic_label: string | null; count: number; }
export interface LeaderboardEntry { symbol: string; post_count_4h: number; sentiment_index_4h: number; buzz_z: number | null; baseline_hourly: number; baseline_samples: number; }
export interface MarketQuote { timestamp: string; price: number; volume: number; market_session: string; }
export interface DeltaData { reference_price: number; latest_price: number; pct_change: number; abs_change: number; }
export interface MetricsData {
  pe_ratio: number | null; beta: number | null; avg_return_1y: number | null;
  inflation_adj_return_1y: number | null; pe_relative_sector: number | null;
  beta_relative_sector: number | null; return_relative_sector: number | null;
  updated_at: string;
}
export interface OpportunityData {
  score: number;
  classification: string;
  color: string;
  strategy: string;
  description: string;
  checklist: string[];
}
export interface CorrelationBucket {
  timestamp: string; positive: number; neutral: number; negative: number;
  priceChange: number | null; futureChange: number | null;
  isMarketOpen: boolean; sentimentIndex: number; sentimentSMA: number;
  rawPrice: number | null; buySignal: boolean | null; buyScore: number | null;
  sentimentMACD?: number | null; sentimentSignal?: number | null; sentimentHist?: number | null;
}
export interface ClosedRegion { start: string; end: string; }
export interface LagSweepValue { lag: number; r: number; }
export interface CorrelationData {
  data: CorrelationBucket[]; closedRegions: ClosedRegion[];
  supportPrice: number; supportPct: number; resistancePrice: number; resistancePct: number;
  maxR: number; bestLag: number; lagSweeps?: LagSweepValue[]; correlationText: string; correlationStrength: string;
  opportunity: OpportunityData | null;
}

export interface DashboardDataProps {
  state: any;
  setters: any;
  computed: any;
}

