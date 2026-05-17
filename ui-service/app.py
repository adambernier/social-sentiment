import streamlit as st
import requests
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import os
import pandas_market_calendars as mcal
from datetime import datetime

API_URL = os.environ.get("API_URL", "http://localhost:8000")

# Initialize market calendar
nyse = mcal.get_calendar('NYSE')

st.set_page_config(page_title="Social Sentiment & Market Dashboard", layout="wide")

# --- Sidebar Filters ---
st.sidebar.header("Settings")
live_mode = st.sidebar.toggle("Live Mode (Auto-refresh)", value=False)

# --- Dynamic Time Window Logic ---
now = datetime.now()
# Fetch recent schedule to find the last close
recent_schedule = nyse.schedule(start_date=now - pd.Timedelta(days=7), end_date=now)
last_close = recent_schedule.iloc[-1]['market_close'].to_pydatetime()

# If it's a weekend or before today's open, the 'last close' is Friday/Yesterday
if now.astimezone(last_close.tzinfo) < last_close:
    # If today is a trading day but we haven't reached the close yet,
    # the 'Last Close' is actually the previous trading day.
    if len(recent_schedule) > 1:
        last_close = recent_schedule.iloc[-2]['market_close'].to_pydatetime()

# Determine if it's currently a weekend for the 'Weekend Digest' mode
is_weekend = now.weekday() >= 5 # 5=Saturday, 6=Sunday

window_options = ["Last 24h", "Last 48h", "Since Last Close"]
if is_weekend:
    window_options.insert(0, "Weekend Digest")

selected_window = st.sidebar.selectbox("Time Window", window_options, index=0)

# Map selection to hours
if selected_window == "Last 24h":
    hours = 24
elif selected_window == "Last 48h":
    hours = 48
else:
    # "Since Last Close" or "Weekend Digest"
    delta = datetime.now(last_close.tzinfo) - last_close
    hours = max(1, int(delta.total_seconds() / 3600))

symbol = st.sidebar.selectbox("Symbol", ["ASTS", "RKLB", "INTC", "NVDA"])
platform = st.sidebar.selectbox("Platform", ["All", "bluesky", "stocktwits", "yahoo"])
platform_param = "" if platform == "All" else f"&platform={platform}"

# --- Data Fetching Functions ---
@st.cache_data(ttl=30)
def fetch_stats(hrs, sym, p_param):
    try:
        url = f"{API_URL}/stats/sentiment?hours={hrs}&symbol={sym}{p_param}"
        res = requests.get(url, timeout=5)
        return res.json() if res.status_code == 200 else []
    except Exception as e:
        return []

@st.cache_data(ttl=30)
def fetch_topic_stats(hrs, sym, p_param):
    try:
        url = f"{API_URL}/stats/topics?hours={hrs}&symbol={sym}{p_param}"
        res = requests.get(url, timeout=5)
        return res.json() if res.status_code == 200 else []
    except Exception as e:
        return []

@st.cache_data(ttl=30)
def fetch_posts(sym, p_param):
    try:
        # Use a larger limit to ensure we have enough for both news and social
        url = f"{API_URL}/posts?limit=1000&symbol={sym}{p_param}"
        res = requests.get(url, timeout=5)
        return res.json() if res.status_code == 200 else []
    except Exception as e:
        return []

@st.cache_data(ttl=30)
def fetch_market_data(sym, hrs):
    try:
        res = requests.get(f"{API_URL}/stats/market?symbol={sym}&hours={hrs}", timeout=5)
        return res.json() if res.status_code == 200 else []
    except Exception as e:
        return []

@st.cache_data(ttl=30)
def fetch_latest_quote(sym):
    try:
        res = requests.get(f"{API_URL}/stats/market/latest?symbol={sym}", timeout=5)
        return res.json() if res.status_code == 200 else None
    except Exception as e:
        return None

@st.cache_data(ttl=300) # Metrics change slowly
def fetch_metrics(sym):
    try:
        res = requests.get(f"{API_URL}/stats/metrics?symbol={sym}", timeout=5)
        return res.json() if res.status_code == 200 else None
    except Exception as e:
        return None

# --- Main Dashboard Fragment ---
@st.fragment(run_every="60s" if live_mode else None)
def dashboard_content():
    stats_data = fetch_stats(hours, symbol, platform_param)
    topic_stats = fetch_topic_stats(hours, symbol, platform_param)
    posts_data = fetch_posts(symbol, platform_param)
    market_data = fetch_market_data(symbol, hours)
    latest_quote = fetch_latest_quote(symbol)
    metrics_data = fetch_metrics(symbol)

    # --- Header Row ---
    header_left, header_right = st.columns([3, 1])
    with header_left:
        st.markdown("<h1 class='dashboard-title'>📊 Social Sentiment & Market Data</h1>", unsafe_allow_html=True)
    
    # --- Data Processing ---
    df_posts = pd.DataFrame()
    if posts_data:
        df_posts = pd.DataFrame(posts_data)
        df_posts['timestamp'] = pd.to_datetime(df_posts['timestamp'], utc=True, format='ISO8601')
        cutoff = datetime.now(df_posts['timestamp'].dt.tz) - pd.Timedelta(hours=hours)
        df_posts = df_posts[df_posts['timestamp'] >= cutoff]

    # --- KPIs Row ---
    col1, col2, col3, col4, col5, col6 = st.columns(6)

    # Sentiment KPIs
    if not df_posts.empty:
        total_posts = len(df_posts)
        pos_count = len(df_posts[df_posts['sentiment'] == 'positive'])
        neg_count = len(df_posts[df_posts['sentiment'] == 'negative'])
        
        col1.metric("Total Mentions", total_posts)
        col2.metric("Bullish", f"{pos_count/total_posts*100:.0f}%" if total_posts else "0%")
        col3.metric("Bearish", f"{neg_count/total_posts*100:.0f}%" if total_posts else "0%")
        
        # Divergence KPI
        df_social = df_posts[df_posts['platform'].isin(['bluesky', 'stocktwits', 'simulator'])]
        df_news = df_posts[df_posts['platform'] == 'yahoo']
        
        if not df_social.empty and not df_news.empty:
            social_bull = len(df_social[df_social['sentiment'] == 'positive']) / len(df_social)
            news_bull = len(df_news[df_news['sentiment'] == 'positive']) / len(df_news)
            diff = (social_bull - news_bull) * 100
            col4.metric("Divergence", f"{diff:+.0f}%", help="Retail Bullishness minus Institutional Bullishness")
        else:
            col4.metric("Divergence", "N/A")
    else:
        col1.metric("Total Mentions", 0)
        col2.metric("Bullish", "0%")
        col3.metric("Bearish", "0%")
        col4.metric("Divergence", "N/A")

    # Market KPIs
    if latest_quote:
        price_val = latest_quote['price']
        vol_val = latest_quote['volume']
        session = latest_quote.get('market_session', 'closed')
        as_of = pd.to_datetime(latest_quote['timestamp'])
        
        # Determine Label and Pulse
        if session == 'regular':
            price_label = "Live Price"
            status_html = '<span class="pulse"></span> **Market Open**'
        elif session == 'pre':
            price_label = "Pre-market"
            status_html = "🟡 **Pre-market**"
        elif session == 'after':
            price_label = "After-hours"
            status_html = "🔵 **After-hours**"
        else:
            # Format nicely: Last Close — Fri May 15, 16:00 ET
            # Convert UTC to ET for display (approx -4h)
            et_time = as_of - pd.Timedelta(hours=4)
            price_label = f"Last Close — {et_time.strftime('%a %b %d, %H:%M')} ET"
            status_html = "⚪ **Market Closed**"

        with header_right:
            st.markdown(f"<div style='text-align: right; margin-top: 15px;'>{status_html}</div>", unsafe_allow_html=True)
        
        # Calculate change from start of window if possible
        price_display = f"${price_val:.2f}"
        if session == 'closed':
            # Use markdown for the metric to allow styling if it's stale
            col5.markdown(f"<p style='font-size: 14px; margin-bottom: -10px;'>{price_label}</p>", unsafe_allow_html=True)
            col5.markdown(f"<h2 class='stale-price' style='margin-top: 0;'>{price_display}</h2>", unsafe_allow_html=True)
        else:
            if market_data:
                df_market = pd.DataFrame(market_data)
                prev_quote_val = df_market.iloc[0]['price']
                change = price_val - prev_quote_val
                col5.metric(f"{symbol} {price_label}", price_display, f"{change:+.2f}")
            else:
                col5.metric(f"{symbol} {price_label}", price_display)
            
        col6.metric("Volume", f"{vol_val:,}")
    else:
        col5.metric(f"{symbol} Price", "N/A")
        col6.metric("Volume", "N/A")

    st.divider()

    # --- Scorecard Section ---
    if metrics_data:
        st.subheader(f"🏷️ {symbol} Relative Performance Scorecard")
        col_card, col_info = st.columns([2, 1])
        
        with col_card:
            # Prepare data for horizontal bar chart
            labels = ["Valuation (P/E)", "Risk (Beta)", "Avg Return (1Y)"]
            # These are the relative scores calculated by the producer
            values = [
                metrics_data.get("pe_relative_sector", 0),
                metrics_data.get("beta_relative_sector", 0),
                metrics_data.get("return_relative_sector", 0)
            ]
            
            # Map values to colors: green for positive, red for negative
            colors = ["#2ECC71" if v > 0 else "#E74C3C" for v in values]
            
            fig_card = go.Figure(go.Bar(
                x=values,
                y=labels,
                orientation='h',
                marker_color=colors,
                text=[f"{v:+.1%}" for v in values],
                textposition='auto',
            ))
            
            # Calculate a symmetrical max range with a minimum of 50% and 15% padding for text
            max_val = max([abs(v) for v in values] + [0.5]) * 1.15
            
            fig_card.update_layout(
                xaxis=dict(title="Relative to Sector (%)", range=[-max_val, max_val], tickformat=".0%"),
                yaxis=dict(autorange="reversed"),
                template="plotly_white",
                height=300,
                margin=dict(l=20, r=20, t=20, b=20),
            )
            
            # Add a vertical line at 0 for reference
            fig_card.add_vline(x=0, line_width=2, line_dash="dash", line_color="gray")
            
            st.plotly_chart(fig_card, use_container_width=True)
            
        with col_info:
            st.markdown("### Fundamental Metrics")
            st.write(f"**P/E Ratio:** {metrics_data.get('pe_ratio', 'N/A')}")
            st.write(f"**Beta:** {metrics_data.get('beta', 'N/A')}")
            st.write(f"**Annual Return:** {metrics_data.get('avg_return_1y', 0):.1%}")
            st.write(f"**Inflation Adjusted:** {metrics_data.get('inflation_adj_return_1y', 0):.1%}")
            st.caption(f"Last updated: {metrics_data.get('updated_at', 'N/A')}")
    else:
        st.info("Scorecard data currently unavailable. The Market Producer will update these metrics shortly.")

    st.divider()

    # --- Correlation Chart ---
    chart_title = f"📈 {symbol} Sentiment vs. Price Correlation"
    if latest_quote and latest_quote.get('market_session') == 'closed':
        chart_title = f"📈 {symbol} Sentiment Activity (Market Closed)"
    
    st.subheader(chart_title)
    
    if not df_posts.empty:
        # Determine bucket frequency based on time window
        freq = '1h' if hours > 6 else '15min'
        df_posts['time_bucket'] = df_posts['timestamp'].dt.floor(freq)
        
        # Group by bucket and sentiment
        ts_df = df_posts.groupby(['time_bucket', 'sentiment']).size().reset_index(name='count')
        
        color_map = {'positive': '#2ECC71', 'neutral': '#95A5A6', 'negative': '#E74C3C'}
        
        fig = px.bar(
            ts_df, x='time_bucket', y='count', color='sentiment',
            color_discrete_map=color_map,
            category_orders={"sentiment": ["positive", "neutral", "negative"]},
            template="plotly_white",
            barmode='stack'
        )
        
        # Add Shading for Closed Hours
        # Get start/end of the chart's time range
        chart_start = df_posts['timestamp'].min()
        chart_end = df_posts['timestamp'].max()
        
        if chart_start and chart_end:
            # Get NYSE schedule for this range
            # Note: nyse.schedule expects dates, so we use chart_start.date() to chart_end.date()
            schedule = nyse.schedule(start_date=chart_start, end_date=chart_end)
            
            # Draw shaded rectangles for each night/weekend
            # We iterate through the days and shade the gaps between market_close and next market_open
            # Plus everything before first open and after last close in the window
            
            # Simple approach: Find periods in ts_df where market is NOT open
            # But even better: Use the actual NYSE schedule to be precise
            
            # 1. Shade from chart_start to first market_open (if it exists)
            # 2. Iterate through schedule rows and shade gaps between market_close and next market_open
            # 3. Shade from last market_close to chart_end
            
            prev_close = chart_start
            for idx, row in schedule.iterrows():
                m_open = row['market_open'].to_pydatetime()
                m_close = row['market_close'].to_pydatetime()
                
                # Shade from prev_close to m_open (if there's a gap)
                if m_open > prev_close:
                    fig.add_vrect(
                        x0=prev_close, x1=m_open,
                        fillcolor="gray", opacity=0.1, line_width=0,
                        layer="below"
                    )
                prev_close = m_close
            
            # Final shade from last close to end of chart
            if chart_end > prev_close:
                fig.add_vrect(
                    x0=prev_close, x1=chart_end,
                    fillcolor="gray", opacity=0.1, line_width=0,
                    layer="below"
                )

        if market_data:
            df_market = pd.DataFrame(market_data)
            df_market['timestamp'] = pd.to_datetime(df_market['timestamp'], utc=True, format='ISO8601')
            
            # Distinguish between regular/active sessions and closed sessions for the line style
            # Split into traces or use a list of line styles
            # For simplicity and clear visual: draw one continuous line, but dashed during 'closed' sessions
            # Actually, Scatter 'line' style can't be changed per-point easily.
            # Best way: multiple traces.
            
            # Separate market data into contiguous segments
            # For this MVP: draw the yellow line. We'll use a single trace but can 
            # make it dashed if ALL points in the window are closed.
            
            # Advanced: Split into segments
            last_idx = 0
            for i in range(1, len(df_market)):
                if df_market.iloc[i]['market_session'] != df_market.iloc[i-1]['market_session']:
                    # Segment change
                    segment = df_market.iloc[last_idx:i+1]
                    is_closed = df_market.iloc[i-1]['market_session'] == 'closed'
                    
                    fig.add_trace(go.Scatter(
                        x=segment['timestamp'], y=segment['price'],
                        name='Price (Closed)' if is_closed else 'Price',
                        yaxis='y2',
                        line=dict(color='#F1C40F', width=4, dash='dot' if is_closed else 'solid'),
                        showlegend=False
                    ))
                    last_idx = i
            
            # Final segment
            segment = df_market.iloc[last_idx:]
            is_closed = df_market.iloc[-1]['market_session'] == 'closed'
            fig.add_trace(go.Scatter(
                x=segment['timestamp'], y=segment['price'],
                name='Price (Closed)' if is_closed else 'Price',
                yaxis='y2',
                line=dict(color='#F1C40F', width=4, dash='dot' if is_closed else 'solid'),
                showlegend=False
            ))
            
            fig.update_layout(
                yaxis2=dict(title="Stock Price ($)", overlaying='y', side='right', showgrid=False)
            )
        
        fig.update_layout(xaxis_title="Time", yaxis_title="Post Count", legend_title="Legend", hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Insufficient data for correlation chart.")

    st.divider()

    # --- Topic Distribution Chart ---
    st.subheader("🏷️ Trending Topics")
    if topic_stats:
        df_topics = pd.DataFrame(topic_stats)
        # Limit to top 10 topics for better visualization
        df_topics = df_topics.head(10)
        
        fig_topics = px.bar(
            df_topics,
            x="count",
            y="topic_label",
            orientation="h",
            color="count",
            color_continuous_scale="Viridis",
            labels={"count": "Mentions", "topic_label": "Topic"},
            template="plotly_white",
            height=400
        )
        
        fig_topics.update_layout(
            yaxis={"autorange": "reversed"},
            coloraxis_showscale=False,
            margin=dict(l=20, r=20, t=20, b=20)
        )
        
        st.plotly_chart(fig_topics, use_container_width=True)
    else:
        st.info("No topic data found for this period.")

    # --- Weekend Digest / Window Summary ---
    st.divider()
    if selected_window == "Weekend Digest":
        st.info(f"✨ **Weekend Digest**: Summarizing all chatter since {last_close.strftime('%A, %b %d at %H:%M')} ET. How should this sentiment shift expectations for Monday's open?")
    elif selected_window == "Since Last Close":
        st.caption(f"Showing data since last close on {last_close.strftime('%b %d, %H:%M')} ET")

    # --- Side-by-Side Feeds ---
    col_social, col_news = st.columns(2)

    with col_social:
        st.subheader("💬 Retail Social Feed")
        df_social = df_posts[df_posts['platform'].isin(['bluesky', 'stocktwits', 'simulator'])]
        if not df_social.empty:
            df_social_view = df_social.head(20).copy()
            df_social_view['timestamp'] = df_social_view['timestamp'].dt.strftime('%H:%M')
            st.dataframe(
                df_social_view[['timestamp', 'platform', 'sentiment', 'text']],
                use_container_width=True, hide_index=True
            )
        else:
            st.info("No social chatter found.")

    with col_news:
        st.subheader("📰 Institutional News")
        df_news = df_posts[df_posts['platform'] == 'yahoo']
        if not df_news.empty:
            df_news_view = df_news.head(20).copy()
            df_news_view['timestamp'] = df_news_view['timestamp'].dt.strftime('%H:%M')
            st.dataframe(
                df_news_view[['timestamp', 'sentiment', 'text']],
                use_container_width=True, hide_index=True
            )
        else:
            st.info("No news headlines found.")

# Execute the fragment
dashboard_content()

# --- Manual Refresh ---
if st.sidebar.button("Clear Cache & Refresh"):
    st.cache_data.clear()
    st.rerun()
