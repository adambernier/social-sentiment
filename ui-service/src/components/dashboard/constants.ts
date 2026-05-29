export const platformColors: Record<string, string> = {
  twitter: 'bg-sky-500/20 text-sky-400 border border-sky-500/10',
  bluesky: 'bg-indigo-500/20 text-indigo-400 border border-indigo-500/10',
  reddit: 'bg-orange-500/20 text-orange-400 border border-orange-500/10',
  yahoo: 'bg-purple-500/20 text-purple-400 border border-purple-500/10',
  stocktwits: 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/10',
  finnhub: 'bg-blue-500/20 text-blue-400 border border-blue-500/10',
  alpaca: 'bg-teal-500/20 text-teal-400 border border-teal-500/10',
  yfinance: 'bg-yellow-500/20 text-yellow-400 border border-yellow-500/10',
};

export const platformLabels: Record<string, string> = {
  twitter: 'X/Twitter',
  reddit: 'Reddit',
  bluesky: 'Bluesky',
  stocktwits: 'Stocktwits',
  finnhub: 'News Article',
  alpaca: 'Alpaca News',
  yfinance: 'Market Data (YF)',
};

export const NEWS_PLATFORMS = new Set(["finnhub", "alpaca"]);
export const isNewsPlatform = (platform: string) => NEWS_PLATFORMS.has(platform);

export const topicColors: Record<string, string> = {
  "Earnings & Guidance": "bg-emerald-500/20 text-emerald-300 border border-emerald-500/30",
  "Fed & Macro": "bg-amber-500/20 text-amber-300 border border-amber-500/30",
  "Technical Analysis": "bg-blue-500/20 text-blue-300 border border-blue-500/30",
  "AI & Compute": "bg-indigo-500/20 text-indigo-300 border border-indigo-500/30",
  "Space & Satellite": "bg-cyan-500/20 text-cyan-300 border border-cyan-500/30",
  "Management & Insider": "bg-purple-500/20 text-purple-300 border border-purple-500/30",
  "M&A & Partnerships": "bg-fuchsia-500/20 text-fuchsia-300 border border-fuchsia-500/30",
  "Options & Volatility": "bg-rose-500/20 text-rose-300 border border-rose-500/30",
  "General / Outlier": "bg-slate-500/20 text-slate-300 border border-slate-500/30",
};

export const topicProgressColors: Record<string, string> = {
  "Earnings & Guidance": "bg-emerald-500",
  "Fed & Macro": "bg-amber-500",
  "Technical Analysis": "bg-blue-500",
  "AI & Compute": "bg-indigo-500",
  "Space & Satellite": "bg-cyan-500",
  "Management & Insider": "bg-purple-500",
  "M&A & Partnerships": "bg-fuchsia-500",
  "Options & Volatility": "bg-rose-500",
  "General / Outlier": "bg-slate-600",
  "General Chat": "bg-slate-600",
};
