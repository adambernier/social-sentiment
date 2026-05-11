import streamlit as st
import requests
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import os
from datetime import datetime

API_URL = os.environ.get("API_URL", "http://localhost:8000")

st.set_page_config(page_title="Social Sentiment & Market Dashboard", layout="wide")

st.title("📊 Social Sentiment & Market Data")

# --- Sidebar Filters ---
st.sidebar.header("Settings")
live_mode = st.sidebar.toggle("Live Mode (Auto-refresh)", value=False)
hours = st.sidebar.slider("Time Window (Hours)", 1, 48, 24)
symbol = st.sidebar.selectbox("Symbol", ["ASTS", "RKLB", "INTC"])
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
    posts_data = fetch_posts(symbol, platform_param)
    market_data = fetch_market_data(symbol, hours)
    latest_quote = fetch_latest_quote(symbol)
    metrics_data = fetch_metrics(symbol)

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
        # Use market_data if available for the change delta, else use latest_quote
        price_val = latest_quote['price']
        vol_val = latest_quote['volume']
        
        # Calculate change from start of window if possible
        if market_data:
            df_market = pd.DataFrame(market_data)
            prev_quote_val = df_market.iloc[0]['price']
            change = price_val - prev_quote_val
            col5.metric(f"{symbol} Price", f"${price_val:.2f}", f"{change:+.2f}")
        else:
            col5.metric(f"{symbol} Price", f"${price_val:.2f}")
            
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
    st.subheader(f"📈 {symbol} Sentiment vs. Price Correlation")
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
        
        if market_data:
            df_market = pd.DataFrame(market_data)
            df_market['timestamp'] = pd.to_datetime(df_market['timestamp'], utc=True, format='ISO8601')
            
            fig.add_trace(go.Scatter(
                x=df_market['timestamp'], y=df_market['price'], 
                name=f'{symbol} Price', yaxis='y2',
                line=dict(color='#F1C40F', width=4)
            ))
            
            fig.update_layout(
                yaxis2=dict(title="Stock Price ($)", overlaying='y', side='right', showgrid=False)
            )
        
        fig.update_layout(xaxis_title="Time", yaxis_title="Post Count", legend_title="Legend", hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Insufficient data for correlation chart.")

    st.divider()

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
