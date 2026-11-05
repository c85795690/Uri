"""
🐾 OKX Order Flow - 可愛版即時多空監控與 K 線盈虧面板
"""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import numpy as np
from datetime import datetime

# ==========================================
# 1. 網頁初始化與可愛風主題配置 (Lovely Pink Theme)
# ==========================================
st.set_page_config(page_title="🐱 OKX Order Flow 貓咪多空雷達", layout="wide")

st.markdown("""
    <style>
    .reportview-container { background: #FFF0F5; } /* 輕柔粉底 */
    .big-title { font-size:32px !important; font-weight: bold; color: #FF69B4; text-align: center; margin-bottom: 20px; }
    .metric-box { background-color: #FFFFFF; border-radius: 15px; padding: 15px; border: 2px solid #FFB6C1; box-shadow: 2px 2px 10px rgba(255,182,193,0.3); }
    </style>
""", unsafe_allow_html=True)

st.markdown("<div class='big-title'>🐾 🎀 OKX 訂單流即時多空雷達 K 線面板 🎀 🐾</div>", unsafe_allow_html=True)

# ==========================================
# 2. 模擬接收後台實時進出場與 K 線數據 (對接實盤/DEMO)
# ==========================================
@st.cache_data
def get_mock_trading_data():
    # 建立時間軸以生成 K 線
    dates = pd.date_range(start="2026-11-06 00:00", periods=50, freq="1min")
    np.random.seed(42)
    prices = np.random.randn(50).cumsum() + 68000  # 以 BTC 為範例基準價格
    
    df = pd.DataFrame({
        'Open': prices - np.random.rand(50)*10,
        'High': prices + np.random.rand(50)*20,
        'Low': prices - np.random.rand(50)*20,
        'Close': prices,
    }, index=dates)
    return df

df_kline = get_mock_trading_data()

# 模擬後台觸發的訂單流進出場點位 (對齊您系統中 ATR 與進場邏輯)
entry_idx = 35
entry_price = float(df_kline['Open'].iloc[entry_idx])
tp_price = entry_price + 80   # 基於 TAKE_PROFIT_R 計算
sl_price = entry_price - 50   # 基於 ATR_SL_MULTIPLIER 計算
current_price = float(df_kline['Close'].iloc[-1])
pnl_percent = ((current_price - entry_price) / entry_price) * 100
pnl_amount = pnl_percent * 10 # 假設 1000 USDT 倉位

# ==========================================
# 3. 頂部即時多空幣種狀態看板 (可愛版 Metric 區塊)
# ==========================================
col1, col2, col3 = st.columns(3)
with col1:
    st.markdown("<div class='metric-box'>🟢 <b>BTC-USDT-SWAP</b><br>狀態：🎯 訂單流共振做多中<br>盤口不平衡率：<span style='color:green;'>+38.5%</span></div>", unsafe_allow_html=True)
with col2:
    st.markdown("<div class='metric-box'>⚪ <b>ETH-USDT-SWAP</b><br>狀態：💤 盤整暖機中<br>盤口不平衡率：<span style='color:gray;'>+2.1%</span></div>", unsafe_allow_html=True)
with col3:
    st.markdown("<div class='metric-box'>🔴 <b>SOL-USDT-SWAP</b><br>狀態：🚨 空頭蓄勢中<br>盤口不平衡率：<span style='color:red;'>-31.2%</span></div>", unsafe_allow_html=True)

st.write("")

# ==========================================
# 4. 繪製 TradingView 風格 K 線圖，並加上 Entry / TP / SL 標籤
# ==========================================
fig = go.Figure()

# 繪製經典 Candlestick (自訂蠟燭顏色迎合可愛風)
fig.add_trace(go.Candlestick(
    x=df_kline.index,
    open=df_kline['Open'], high=df_kline['High'],
    low=df_kline['Low'], close=df_kline['Close'],
    increasing_line_color='#FF6B8B', increasing_fill_color='#FFB6C1', # 粉紅上漲燭
    decreasing_line_color='#4D96FF', decreasing_fill_color='#6BCB77', # 粉藍下跌燭
    name='K 線行情'
))

# 標註 Entry 虛線與標籤
fig.add_hline(y=entry_price, line_dash="dash", line_color="#4D96FF", line_width=2, 
              annotation_text=f" 🎫 Entry 進場點: {entry_price:.1f}", annotation_position="top left", annotation_font_color="#4D96FF")

# 標註 TP 虛線與標籤
fig.add_hline(y=tp_price, line_dash="dash", line_color="#6BCB77", line_width=2, 
              annotation_text=f" 🦄 TP 止盈目標: {tp_price:.1f}", annotation_position="top left", annotation_font_color="#6BCB77")

# 標註 SL 虛線與標籤
fig.add_hline(y=sl_price, line_dash="dash", line_color="#FF6B6B", line_width=2, 
              annotation_text=f" 🌋 SL 💥 止損線: {sl_price:.1f}", annotation_position="bottom left", annotation_font_color="#FF6B6B")

# 5. 進出盈虧實時動態標籤 (動態跟隨最新價格)
pnl_color = "#FF6B8B" if pnl_percent >= 0 else "#4D96FF"
pnl_emoji = "🎉 ✨ 賺錢囉！" if pnl_percent >= 0 else "😿 😭 補血中"

fig.add_annotation(
    x=df_kline.index[-1], y=current_price,
    text=f" {pnl_emoji}<br>實時盈虧: {pnl_percent:+.2f}% ({pnl_amount:+.2f} USDT)",
    showarrow=True, arrowhead=2, arrowcolor=pnl_color,
    ax=60, ay=-40,
    bordercolor=pnl_color, borderwidth=2, borderpad=6,
    bgcolor="#FFFFFF", opacity=0.95, font=dict(color=pnl_color, size=14)
)

# 圖表樣式微調對齊專業外觀
fig.update_layout(
    height=650, template="plotly_white",
    xaxis_rangeslider_visible=False,
    margin=dict(l=20, r=20, t=20, b=20),
    yaxis=dict(title="價格 (USDT)", gridcolor="#FFE4E1")
)

st.plotly_chart(fig, use_container_width=True)
