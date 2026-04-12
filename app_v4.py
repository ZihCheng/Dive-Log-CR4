import streamlit as st
import json
import os
import asyncio
import base64
import pandas as pd
import plotly.graph_objects as go
from bleak import BleakClient
import firebase_admin
from firebase_admin import credentials, firestore

# --- 1. 網頁基本設定與 CSS 魔法 ---
st.set_page_config(page_title="DiveLog Pro Dashboard", page_icon="🤿", layout="wide")

st.markdown("""
<style>
/* --- 介面優化 --- */
[data-testid="stMetricValue"] div { font-size: 1.6rem !important; }
[data-testid="stMetricLabel"] p { font-size: 0.95rem !important; color: #555555 !important; }
div[data-baseweb="select"] > div { cursor: pointer !important; }
div[data-baseweb="select"] input { caret-color: transparent !important; cursor: pointer !important; }
.aria-hidden, a.header-anchor, [data-testid="stHeaderActionElements"] { display: none !important; }


/* === 🎨 更改選中時間的按鈕顏色 === */
div[data-testid="stVerticalBlock"]:has(.time-mask) button[kind="primary"] {
    background-color: #0083B8 !important;
    border-color: #0083B8 !important;
    color: white !important;
}

/* === Expander === */
div[data-testid="stExpander"] {
    border: none !important;
    box-shadow: none !important;
    background: transparent !important;
}

div[data-testid="stExpander"] details {
    border: none !important;
    box-shadow: none !important;
    background: transparent !important;
    padding: 0 !important;
}

div[data-testid="stExpander"] summary {
    padding-left: 0 !important;
    padding-right: 0 !important;
}

/* === prev/next === */
/* 只影響左右導航按鈕 */
div[data-testid="stColumn"]:has(.prev-anchor) button,
div[data-testid="stColumn"]:has(.next-anchor) button {
    opacity: 0.1;
    transition: all 0.3s ease-in-out;
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;

    /* 讓整條都可點 */
    height: 380px !important;
    min-height: 380px !important;
    padding: 0 !important;

    display: flex !important;
    justify-content: center !important;
    align-items: center !important;
}

/* hover 時才明顯 */
div[data-testid="stColumn"]:has(.prev-anchor) button:hover,
div[data-testid="stColumn"]:has(.next-anchor) button:hover {
    opacity: 1;
    transform: scale(1.2);
    color: #555555 !important;
}

/* 箭頭字體 */
div[data-testid="stColumn"]:has(.prev-anchor) button p,
div[data-testid="stColumn"]:has(.next-anchor) button p {
    font-size: 2rem !important;
    font-weight: 700 !important;
    line-height: 1 !important;
    margin: 0 !important;
}
</style>
""", unsafe_allow_html=True)

# ==========================================
# === 📡 Firebase 初始化 (雲端/本機通用) ===
# ==========================================
if not firebase_admin._apps:
    if os.path.exists("firebase_key.json"):
        cred = credentials.Certificate("firebase_key.json")
    else:
        # 準備為之後部署到 Streamlit Cloud 預留的 Secrets 讀取通道
        fb_dict = dict(st.secrets["firebase_service_account"])
        cred = credentials.Certificate(fb_dict)
    firebase_admin.initialize_app(cred)

db = firestore.client()
COLLECTION_NAME = "dive_logs"

# ==========================================
# === 📡 藍牙直接上雲端同步邏輯 ===
# ==========================================
TARGET_ADDRESS = "AC8C7051-E92A-EE9C-4D84-B5B64389EE53"
FFE1_UUID = "f000ffe1-ab12-45ec-84c8-46483f4626e9"

def make_header_cmd(index: int) -> bytes:
    body = bytes([0xC0, 0x02, 0x01, 0x02, index & 0xFF, (index >> 8) & 0xFF])
    return body + bytes([(~(sum(body) & 0xFF)) & 0xFF])

def make_profile_cmd(start_addr: int, length: int) -> bytes:
    body = bytes([0xC0, 0x03, 0x01, 0x06, length & 0xFF, (length >> 8) & 0xFF,
                  start_addr & 0xFF, (start_addr >> 8) & 0xFF, (start_addr >> 16) & 0xFF, (start_addr >> 24) & 0xFF])
    return body + bytes([(~(sum(body) & 0xFF)) & 0xFF])

def parse_header_for_addr(packet: bytes):
    if len(packet) < 161: return None
    p = packet[4:-1]
    idx, l = int.from_bytes(p[0:4], "little"), int.from_bytes(p[8:12], "little")
    if idx == 0 or idx == 4294967295 or l == 4294967295: return None
    return int.from_bytes(p[40:44], "little"), l

def bt_notification_handler(sender, data):
    pkt = bytes(data)
    if pkt.startswith(b'\xc1\x02'):
        st.session_state.bt_session["header"] = pkt
        st.session_state.bt_session["event"].set()
    elif pkt.startswith(b'\xc1\x03') or len(pkt) > 10:
        content = pkt[4:-1] if pkt.startswith(b'\xc1\x03') else pkt
        st.session_state.bt_session["profile"].extend(content)
        st.session_state.bt_session["event"].set()

async def sync_from_watch(status_placeholder):
    # 🚀 徹底拔除 os.makedirs("divelogs")，不再建立本地資料夾
    st.session_state.bt_session = {"header": None, "profile": bytearray(), "event": asyncio.Event()}
    try:
        async with BleakClient(TARGET_ADDRESS) as client:
            status_placeholder.info("🔗 藍牙已連線！正在比對雲端日誌...")
            await client.start_notify(FFE1_UUID, bt_notification_handler)
            await asyncio.sleep(1.0)
            
            # 🚀 從 Firebase 取得現有日誌 ID，用來判斷哪些不需要下載
            existing_ids = [doc.id for doc in db.collection(COLLECTION_NAME).stream()]
            
            dive_index, empty_count, new_count = 1, 0, 0
            while True:
                doc_id = f"dive_{dive_index:03d}"
                # 如果 Firebase 已經有這筆紀錄，直接跳過
                if doc_id in existing_ids: 
                    dive_index += 1; continue
                
                status_placeholder.warning(f"🔍 檢查手錶第 {dive_index} 潛...")
                st.session_state.bt_session["header"] = None
                st.session_state.bt_session["event"].clear()
                await client.write_gatt_char(FFE1_UUID, make_header_cmd(dive_index), response=False)
                
                try:
                    await asyncio.wait_for(st.session_state.bt_session["event"].wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    empty_count += 1
                    if empty_count >= 3: break
                    dive_index += 1; continue
                
                addr_info = parse_header_for_addr(st.session_state.bt_session["header"])
                if not addr_info: break
                
                st.session_state.bt_session["profile"].clear()
                offset, profile_len = 0, addr_info[1]
                if profile_len > 0:
                    status_placeholder.warning(f"📥 正在下載並上傳第 {dive_index} 潛至資料庫...")
                    while offset < profile_len:
                        st.session_state.bt_session["event"].clear()
                        await client.write_gatt_char(FFE1_UUID, make_profile_cmd(addr_info[0] + offset, 128), response=False)
                        try:
                            await asyncio.wait_for(st.session_state.bt_session["event"].wait(), timeout=3.0)
                        except asyncio.TimeoutError: continue
                        offset += 128; await asyncio.sleep(0.15)
                    
                    # 🚀 將資料直接寫入 Firebase，不再呼叫 json.dump 存本地檔案
                    final_hex = st.session_state.bt_session["profile"][:profile_len].hex()
                    log_data = {
                        "header_hex": st.session_state.bt_session["header"].hex(),
                        "profile_hex_list": [final_hex],
                        "sync_time": firestore.SERVER_TIMESTAMP
                    }
                    db.collection(COLLECTION_NAME).document(doc_id).set(log_data)
                    new_count += 1
                    
                dive_index += 1; empty_count = 0
                
            await client.stop_notify(FFE1_UUID); return True, new_count
    except Exception as e:
        msg = str(e)
        if "not found" in msg.lower(): return False, "找不到手錶，請確認藍牙已開啟。"
        return False, f"連線異常: {msg}"

# --- 2. 解析工具與純雲端資料載入 ---
def format_duration(total_sec):
    h, m, s = int(total_sec//3600), int((total_sec%3600)//60), int(total_sec%60)
    return f"{h} 小時 {m} 分" if h > 0 else f"{m} 分 {s} 秒" if m > 0 else f"{s} 秒"

def parse_header(payload_hex):
    p = bytes.fromhex(payload_hex)[4:-1]
    mode = "Scuba Diving" if p[4] == 0 else "Free Diving" if p[4] == 2 else "Gauge Mode"
    date_str, time_str = f"20{p[12]:02d}-{p[13]:02d}-{p[14]:02d}", f"{p[15]:02d}:{p[16]:02d}"
    itv = max(1, int.from_bytes(p[24:28], "little"))
    depth = (int.from_bytes(p[28:32], "little") - 1000) / 100.0
    return {"mode": mode, "date": date_str, "time": time_str, "sampling_rate": itv, "max_depth": depth, "cns_max": p[76]}

# 🚀 100% 依賴 Firebase 的讀取機制
@st.cache_data(show_spinner="從雲端載入數據...")
def load_all_data_from_cloud():
    stats = {"Scuba Diving": {"count": 0, "total_sec": 0, "max_depth": 0.0}, "Free Diving": {"count": 0, "total_sec": 0, "max_depth": 0.0}}
    all_logs = []
    
    docs = db.collection(COLLECTION_NAME).stream()
    for doc in docs:
        data = doc.to_dict()
        info = parse_header(data["header_hex"])
        all_logs.append({
            "filename": doc.id, 
            "header_hex": data["header_hex"], 
            **info, 
            "profile_hex_list": data.get("profile_hex_list", [])
        })
        
    if not all_logs: return None, None, None
    
    all_logs.sort(key=lambda x: (x["date"], x["time"]))
    db_index, flat_logs, mode_counters = {}, {"Scuba Diving":[], "Free Diving":[], "Gauge Mode":[]}, {"Scuba Diving":0, "Free Diving":0, "Gauge Mode":0}
    
    for log in all_logs:
        m, d, t = log["mode"], log["date"], log["time"]
        mode_counters[m] += 1
        if m not in db_index: db_index[m] = {}
        if d not in db_index[m]: db_index[m][d] = {}
        db_index[m][d][t] = {"fname": log["filename"], "num": mode_counters[m]}
        # 把整包 Firebase 讀下來的 log 塞進 cloud_data 供後續使用
        flat_logs[m].append({"date": d, "time": t, "num": mode_counters[m], "cloud_data": log})
        if m in stats:
            stats[m]["count"] += 1
            itv = 0.5 if "Free" in m else log["sampling_rate"]
            stats[m]["total_sec"] += max(0, sum(len(h) for h in log["profile_hex_list"]) // 2 - 2) // 6 * itv
            stats[m]["max_depth"] = max(stats[m]["max_depth"], log["max_depth"])
    return stats, db_index, flat_logs

def render_plotly_profile_chart(profile_data, is_free: bool):
    df = pd.DataFrame(profile_data).copy()

    x_col = "sec" if is_free else "min"
    x_title = "Time (sec)" if is_free else "Time (min)"

    df["time_label"] = df["sec"].apply(lambda s: f"{int(s//60):02d}:{int(s%60):02d}")

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df[x_col],
        y=df["depth"],
        mode="lines",
        name=" ",
        fill="tozeroy",
        line=dict(width=2.5, color="steelblue"),
        customdata=df[["temp", "time_label"]],
        hovertemplate=("<b>%{customdata[1]}</b><br> Depth: %{y:.1f} m<br> <extra></extra>")
    ))

    fig.add_trace(go.Scatter(
        x=df[x_col],
        y=df["temp"],
        mode="lines",
        name=" ",
        yaxis="y2",
        line=dict(width=2, dash="dot", color="indianred"),
        hovertemplate=("Temp: %{y:.1f} °C<br> <extra></extra>")
    ))

    fig.update_layout(
        height=320,
        margin=dict(l=10, r=10, t=10, b=10),
        hovermode="x unified",
        showlegend=False,
        xaxis=dict(title=x_title),
        yaxis=dict(autorange="reversed"),
        yaxis2=dict(overlaying="y", side="right"),
        hoverlabel=dict(bgcolor="white", font_size=14,)
    )

    # 鎖定縮放 / 拖曳
    fig.update_xaxes(fixedrange=True)
    fig.update_yaxes(fixedrange=True)

    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False, "scrollZoom": False, "doubleClick": False})
# --- 3. 狀態管理 Callback ---
def on_mode_change():
    m = st.session_state.nav_mode
    if m in db_index:
        d = sorted(list(db_index[m].keys()), reverse=True)[0]
        st.session_state.nav_date, st.session_state.nav_time = d, sorted(list(db_index[m][d].keys()))[0]

def on_date_change():
    m, d = st.session_state.nav_mode, st.session_state.nav_date
    if m in db_index and d in db_index[m]:
        st.session_state.nav_time = sorted(list(db_index[m][d].keys()))[0]

def navigate_to(date, time):
    st.session_state.nav_date, st.session_state.nav_time = date, time

def set_time(t):
    st.session_state.nav_time = t

# --- 4. 側邊欄：導航與連線同步 ---
# 🚀 全面採用從 Firebase 讀取的方法
global_stats, db_index, flat_logs = load_all_data_from_cloud()

st.sidebar.header("📅 尋找日誌")
if db_index:
    modes = sorted(list(db_index.keys()))
    if "nav_mode" not in st.session_state:
        st.session_state.nav_mode = modes[0]
        st.session_state.nav_date = sorted(list(db_index[modes[0]].keys()), reverse=True)[0]
        st.session_state.nav_time = sorted(list(db_index[modes[0]][st.session_state.nav_date].keys()))[0]
    st.sidebar.selectbox("• 模式", modes, key="nav_mode", on_change=on_mode_change)
    st.sidebar.selectbox("• 日期", sorted(list(db_index[st.session_state.nav_mode].keys()), reverse=True), key="nav_date", on_change=on_date_change)
    available_times = sorted(list(db_index[st.session_state.nav_mode][st.session_state.nav_date].keys()))
    st.sidebar.write(""); st.sidebar.markdown("**• 下潛時間**")
    t_cont = st.sidebar.container(height=200, border=False)
    with t_cont:
        st.markdown('<div id="time-nav-anchor" class="time-mask"></div>', unsafe_allow_html=True)
        for t in available_times:
            b_type = "primary" if st.session_state.nav_time == t else "secondary"
            st.button(f"⏱️ {t}", key=f"s_{t}", type=b_type, width="stretch", on_click=set_time, args=(t,))
    js = f"""
    <script>
    (() => {{
        const target = "⏱️ {st.session_state.nav_time}";
        const FLAG = "__time_nav_scroll_running__";
    
        if (window[FLAG]) return;
        window[FLAG] = true;
    
        function applyMaskAndScroll() {{
            const root = document;
    
            const anchor = root.getElementById("time-nav-anchor");
            if (!anchor) return false;
    
            // 找到包住時間按鈕的區塊
            const block = anchor.closest('div[data-testid="stVerticalBlock"]');
            if (!block) return false;
    
            // 套遮罩效果
            block.style.WebkitMaskImage = "linear-gradient(to bottom, transparent 0%, black 15%, black 88%, transparent 100%)";
            block.style.maskImage = "linear-gradient(to bottom, transparent 0%, black 15%, black 88%, transparent 100%)";
            block.style.paddingBottom = "20px";
    
            // 只在這個 block 內找時間按鈕，不掃整個 sidebar
            const btns = block.querySelectorAll("button");
            for (const b of btns) {{
                const txt = (b.innerText || "").trim();
                if (txt.includes(target)) {{
                    b.scrollIntoView({{behavior: "smooth", block: "center", inline: "nearest"}});
                    return true;
                }}
            }}
            return false;
        }}
    
        let tries = 0;
        const maxTries = 50;
    
        const timer = setInterval(() => {{
            tries += 1;
            const ok = applyMaskAndScroll();
    
            if (ok || tries >= maxTries) {{
                clearInterval(timer);
                window[FLAG] = false;
            }}
        }}, 200);
    
        // 額外監聽 sidebar 內容變動，讓 rerun / layout 更新後還能補捲動
        const sidebar = document.querySelector('[data-testid="stSidebar"]');
        if (sidebar) {{
            const observer = new MutationObserver(() => {{
                applyMaskAndScroll();
            }});
    
            observer.observe(sidebar, {{
                childList: true,
                subtree: true
            }});
    
            // 8 秒後自動關閉 observer，避免長期掛著
            setTimeout(() => observer.disconnect(), 8000);
        }}
    }})();
    </script>
    """
    with st.sidebar:
        st.html(js, unsafe_allow_javascript=True, width="content")

st.sidebar.header("🔄 資料同步")
sync_btn = st.sidebar.button("從手錶連線並同步", use_container_width=True, type="primary")
msg_cont = st.sidebar.container()
if "sync_msg" in st.session_state:
    msg_cont.success(st.session_state.sync_msg); del st.session_state.sync_msg
if sync_btn:
    with msg_cont:
        status = st.empty()
        ok, res = asyncio.run(sync_from_watch(status))
        if ok: st.session_state.sync_msg = f"✅ 完成！雲端已新增 {res} 筆。"; st.cache_data.clear(); st.rerun()
        else: status.empty(); st.error(res)

# --- 5. 主畫面展示 ---
if global_stats and "nav_mode" in st.session_state:
    with st.expander("📈 Summary", expanded=False):
        sc, fc = st.columns(2)
        with sc:
            st.markdown("#### 🐠 Scuba Diving")
            c1, c2, c3 = st.columns(3)
            c1.metric("次數", f"{global_stats['Scuba Diving']['count']} 支"); c2.metric("總時長", format_duration(global_stats['Scuba Diving']['total_sec'])); c3.metric("最大深度", f"{global_stats['Scuba Diving']['max_depth']:.1f} m")
        with fc:
            st.markdown("#### 🧜‍♀️ Free Diving")
            c1, c2, c3 = st.columns(3)
            c1.metric("次數", f"{global_stats['Free Diving']['count']} 次"); c2.metric("總時長", format_duration(global_stats['Free Diving']['total_sec'])); c3.metric("最大深度", f"{global_stats['Free Diving']['max_depth']:.1f} m")

    curr_list = flat_logs[st.session_state.nav_mode]
    idx = next((i for i, l in enumerate(curr_list) if l["date"] == st.session_state.nav_date and l["time"] == st.session_state.nav_time), 0)

    e = db_index[st.session_state.nav_mode][st.session_state.nav_date][st.session_state.nav_time]
    st.markdown(f"<h3 style='text-align: center; margin-top: 0;'>📊 {st.session_state.nav_mode} #{e['num']} | {st.session_state.nav_date} {st.session_state.nav_time}</h3>", unsafe_allow_html=True)

    # 🚀 直接從雲端資料字典提取內容，完全不依賴 open() 開啟本地檔案
    log_entry = curr_list[idx]["cloud_data"]
    info = parse_header(log_entry["header_hex"])
    raw_bytes = b"".join(bytes.fromhex(h) for h in log_entry["profile_hex_list"])[2:]
    p_data, sec, marker, itv = [], 0.0, None, (0.5 if "Free" in st.session_state.nav_mode else info["sampling_rate"])
    while len(raw_bytes) >= 6:
        chunk = raw_bytes[:6]; raw_bytes = raw_bytes[1:]
        if marker is None or chunk[0] in [marker, (marker-1)%256]:
            d_r, t_r = int.from_bytes(chunk[1:3],"little"), int.from_bytes(chunk[3:5],"little")
            if 100 <= t_r <= 450 and 900 <= d_r <= 16000:
                marker = chunk[0]; p_data.append({"sec":sec, "min":round(sec/60,1), "depth":(d_r-1000)/100.0, "temp":t_r/10.0})
                sec += itv; raw_bytes = raw_bytes[5:]
    
    if p_data:
        depths = [d['depth'] for d in p_data]
        dive_time, max_d, min_t = p_data[-1]['sec'], max(depths), min(d['temp'] for d in p_data)
        
        st.markdown('<div class="chart-nav-wrap">', unsafe_allow_html=True)
        nav1, nav2, nav3 = st.columns([1, 12, 1], vertical_alignment="center", gap=None, border=False)
        with nav1:
            st.markdown('<div class="prev-anchor"></div>', unsafe_allow_html=True)
            with st.container(border=False):
                if idx > 0: st.button(label=" ", icon="◀", key="prev", type="secondary", width="stretch", on_click=navigate_to, args=(curr_list[idx-1]["date"], curr_list[idx-1]["time"]))
        with nav2:
            if "Free" in st.session_state.nav_mode:
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("潛水時長", format_duration(dive_time)); m2.metric("最大深度", f"{max_d:.1f} m"); m3.metric("下潛時間", f"{int(p_data[depths.index(max_d)]['sec'])} 秒"); m4.metric("最低溫度", f"{min_t:.1f} °C")
            else:
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("潛水時長", format_duration(dive_time)); m2.metric("最大深度", f"{max_d:.1f} m"); m3.metric("平均深度", f"{sum(depths)/len(depths):.1f} m"); m4.metric("最低溫度", f"{min_t:.1f} °C"); m5.metric("Max CNS", f"{info['cns_max']} %")
            render_plotly_profile_chart(p_data, "Free" in st.session_state.nav_mode)
        with nav3:
            st.markdown('<div class="next-anchor"></div>', unsafe_allow_html=True)
            with st.container(border=False):
                if idx < len(curr_list)-1: st.button(label=" ", icon="▶", key="next", type="secondary", width="stretch", on_click=navigate_to, args=(curr_list[idx+1]["date"], curr_list[idx+1]["time"]))
        st.markdown('</div>', unsafe_allow_html=True)
else:
    st.info("👋 歡迎！雲端尚未有任何紀錄，請點擊左側「從手錶連線並同步」。")
