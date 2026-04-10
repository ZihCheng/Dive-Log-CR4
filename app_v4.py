import streamlit as st
import json
import os
import asyncio
import base64
# 只有在本機環境且有藍牙硬體時，bleak 才會成功運作
try:
    from bleak import BleakClient
    HAS_BLE = True
except Exception:
    HAS_BLE = False

import firebase_admin
from firebase_admin import credentials, firestore

# --- 1. Firebase 初始化 (修正雲端讀取邏輯) ---
if not firebase_admin._apps:
    try:
        if os.path.exists("firebase_key.json"):
            # A. 本機測試模式：讀取檔案
            cred = credentials.Certificate("firebase_key.json")
        else:
            # B. 雲端部署模式：從 Streamlit Secrets 讀取 (這解決了截圖中的 FileNotFoundError)
            fb_secrets = dict(st.secrets["firebase_service_account"])
            cred = credentials.Certificate(fb_secrets)
        firebase_admin.initialize_app(cred)
    except Exception as e:
        st.error(f"❌ Firebase 初始化失敗：{str(e)}")
        st.stop()

db = firestore.client()
COLLECTION_NAME = "dive_logs"

# --- 2. 網頁 CSS 魔法 (海洋藍 & 介面優化) ---
st.set_page_config(page_title="DiveLog Pro Dashboard", page_icon="🤿", layout="wide")
st.markdown("""
<style>
[data-testid="stMetricValue"] div { font-size: 1.6rem !important; }
[data-testid="stMetricLabel"] p { font-size: 0.95rem !important; color: #555555 !important; }
div[data-baseweb="select"] > div { cursor: pointer !important; }
div[data-baseweb="select"] input { caret-color: transparent !important; cursor: pointer !important; }
.aria-hidden, a.header-anchor, [data-testid="stHeaderActionElements"] { display: none !important; }
[data-testid="column"]:nth-child(1) button, [data-testid="column"]:nth-child(3) button {
    opacity: 0.1; transition: all 0.3s ease; border: none !important; background: transparent; font-size: 1.5rem;
}
[data-testid="column"]:nth-child(1) button:hover, [data-testid="column"]:nth-child(3) button:hover {
    opacity: 1; transform: scale(1.2); color: #FF4B4B !important; 
}
/* 🎨 選中的時間按鈕顏色 */
div[data-testid="stVerticalBlock"]:has(.time-mask) button[kind="primary"] {
    background-color: #0083B8 !important; border-color: #0083B8 !important; color: white !important;
}
</style>
""", unsafe_allow_html=True)

# ==========================================
# === 📡 藍牙同步核心邏輯 (與同步到 Firebase) ===
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

async def sync_from_watch(status_placeholder):
    if not HAS_BLE: return False, "此環境不支援藍牙同步，請在本機電腦執行。"
    st.session_state.bt_session = {"header": None, "profile": bytearray(), "event": asyncio.Event()}
    
    def handler(s, d):
        pkt = bytes(d)
        if pkt.startswith(b'\xc1\x02'):
            st.session_state.bt_session["header"] = pkt
            st.session_state.bt_session["event"].set()
        elif pkt.startswith(b'\xc1\x03') or len(pkt) > 10:
            content = pkt[4:-1] if pkt.startswith(b'\xc1\x03') else pkt
            st.session_state.bt_session["profile"].extend(content)
            st.session_state.bt_session["event"].set()

    try:
        async with BleakClient(TARGET_ADDRESS) as client:
            status_placeholder.info("🔗 藍牙連線成功！正在比對雲端日誌...")
            await client.start_notify(FFE1_UUID, handler)
            await asyncio.sleep(1.0)
            
            exist_ids = [doc.id for doc in db.collection(COLLECTION_NAME).stream()]
            idx, empty, new_count = 1, 0, 0
            while True:
                doc_id = f"dive_{idx:03d}"
                if doc_id in exist_ids: idx += 1; continue
                
                status_placeholder.warning(f"🔍 檢查手錶第 {idx} 潛...")
                st.session_state.bt_session["header"] = None
                st.session_state.bt_session["event"].clear()
                await client.write_gatt_char(FFE1_UUID, make_header_cmd(idx), response=False)
                
                try: await asyncio.wait_for(st.session_state.bt_session["event"].wait(), timeout=3.0)
                except: empty += 1
                if empty >= 3: break
                
                header = st.session_state.bt_session["header"]
                if not header: break
                p = header[4:-1]
                l = int.from_bytes(p[8:12], "little")
                addr = int.from_bytes(p[40:44], "little")
                
                if l > 0:
                    status_placeholder.warning(f"📥 下載並上傳第 {idx} 潛...")
                    st.session_state.bt_session["profile"].clear()
                    offset = 0
                    while offset < l:
                        st.session_state.bt_session["event"].clear()
                        await client.write_gatt_char(FFE1_UUID, make_profile_cmd(addr + offset, 128), response=False)
                        try: await asyncio.wait_for(st.session_state.bt_session["event"].wait(), timeout=2.5)
                        except: continue
                        offset += 128
                        await asyncio.sleep(0.1)
                    
                    # 🚀 直接寫入 Firebase (100% 無本地暫存檔案)
                    db.collection(COLLECTION_NAME).document(doc_id).set({
                        "header_hex": header.hex(),
                        "profile_hex_list": [st.session_state.bt_session["profile"][:l].hex()],
                        "sync_time": firestore.SERVER_TIMESTAMP
                    })
                    new_count += 1
                idx += 1; empty = 0
            return True, new_count
    except Exception as e: return False, str(e)

# --- 3. 解析與資料載入 ---
def format_duration(total_sec):
    h, m, s = int(total_sec//3600), int((total_sec%3600)//60), int(total_sec%60)
    return f"{h} 小時 {m} 分" if h > 0 else f"{m} 分 {s} 秒" if m > 0 else f"{s} 秒"

def parse_header(payload_hex):
    p = bytes.fromhex(payload_hex)[4:-1]
    mode = "Scuba Diving" if p[4] == 0 else "Free Diving" if p[4] == 2 else "Gauge Mode"
    depth = (int.from_bytes(p[28:32], "little") - 1000) / 100.0
    return {"mode": mode, "date": f"20{p[12]:02d}-{p[13]:02d}-{p[14]:02d}", "time": f"{p[15]:02d}:{p[16]:02d}", 
            "sampling_rate": max(1, int.from_bytes(p[24:28], "little")), "max_depth": depth, "cns_max": p[76]}

@st.cache_data(show_spinner=False)
def load_data():
    docs = db.collection(COLLECTION_NAME).stream()
    stats = {"Scuba Diving": {"count": 0, "total_sec": 0, "max_depth": 0.0}, "Free Diving": {"count": 0, "total_sec": 0, "max_depth": 0.0}}
    idx_db, flat, mode_cnt, all_logs = {}, {"Scuba Diving":[], "Free Diving":[], "Gauge Mode":[]}, {"Scuba Diving":0, "Free Diving":0, "Gauge Mode":0}, []
    
    for d in docs:
        raw_data = d.to_dict()
        info = parse_header(raw_data["header_hex"])
        all_logs.append({"filename": d.id, "header_hex": raw_data["header_hex"], **info, "profile_hex_list": raw_data.get("profile_hex_list", [])})
    
    if not all_logs: return None, None, None
    all_logs.sort(key=lambda x: (x["date"], x["time"]))
    
    for l in all_logs:
        m, d, t = l["mode"], l["date"], l["time"]
        mode_cnt[m] += 1
        if m not in idx_db: idx_db[m] = {}
        if d not in idx_db[m]: idx_db[m][d] = {}
        idx_db[m][d][t] = {"fname": l["filename"], "num": mode_cnt[m]}
        flat[m].append({"date": d, "time": t, "num": mode_cnt[m], "cloud_data": l})
        if m in stats:
            stats[m]["count"] += 1
            itv = 0.5 if "Free" in m else l["sampling_rate"]
            stats[m]["total_sec"] += max(0, sum(len(h) for h in l["profile_hex_list"]) // 2 - 2) // 6 * itv
            stats[m]["max_depth"] = max(stats[m]["max_depth"], l["max_depth"])
    return stats, idx_db, flat

# D3 圖表 (解決 &deg;C 筆字問題)
def d3_plot(profile_data, is_free):
    chart_data = json.dumps(profile_data)
    time_key = "sec" if is_free else "min"
    return f"""
    <!DOCTYPE html><html><head><script src="https://d3js.org/d3.v7.min.js"></script>
    <style> body {{ margin: 0; font-family: sans-serif; overflow: hidden; }} #data-display {{ text-align: center; height: 24px; color: #666; margin-top:-5px; }} .axis text {{ font-size: 11px; }} </style></head><body>
    <div id="chart-container"></div><div id="data-display"><i></i></div>
    <script>
        const data = {chart_data}, tk = '{time_key}';
        const container = d3.select("#chart-container"), width = container.node().getBoundingClientRect().width || 1000, height = 340;
        const margin = {{top:20, right:60, bottom:40, left:60}}, iW = width-margin.left-margin.right, iH = height-margin.top-margin.bottom;
        const svg = container.append("svg").attr("width",width).attr("height",height).append("g").attr("transform",`translate(${{margin.left}},${{margin.top}})`);
        const x = d3.scaleLinear().domain(d3.extent(data, d=>d[tk])).range([0,iW]), yD = d3.scaleLinear().domain([0,d3.max(data, d=>d.depth)]).range([0,iH]), yT = d3.scaleLinear().domain([d3.min(data, d=>d.temp)-1, d3.max(data, d=>d.temp)+1]).range([iH,0]);
        svg.append("g").attr("transform",`translate(0,${{iH}})`).call(d3.axisBottom(x)); svg.append("g").call(d3.axisLeft(yD)); svg.append("g").attr("transform",`translate(${{iW}},0)`).call(d3.axisRight(yT));
        svg.append("path").datum(data).attr("fill","steelblue").attr("opacity",0.3).attr("d",d3.area().x(d=>x(d[tk])).y0(0).y1(d=>yD(d.depth)).curve(d3.curveMonotoneX));
        svg.append("path").datum(data).attr("fill","none").attr("stroke","steelblue").attr("stroke-width",2.5).attr("d",d3.line().x(d=>x(d[tk])).y(d=>yD(d.depth)).curve(d3.curveMonotoneX));
        svg.append("path").datum(data).attr("fill","none").attr("stroke","indianred").attr("stroke-width",2).attr("stroke-dasharray","4,4").attr("d",d3.line().x(d=>x(d[tk])).y(d=>yT(d.temp)).curve(d3.curveMonotoneX));
        const focus = svg.append("g").style("display","none"); focus.append("line").attr("stroke","#888").attr("stroke-dasharray","4,4").attr("y1",0).attr("y2",iH).attr("id","fL");
        const fD = focus.append("circle").attr("fill","steelblue").attr("stroke","white").attr("stroke-width",2).attr("r",5), fT = focus.append("circle").attr("fill","indianred").attr("stroke","white").attr("stroke-width",2).attr("r",5);
        svg.append("rect").attr("width",iW).attr("height",iH).style("fill","none").style("pointer-events","all")
            .on("mouseover",()=>focus.style("display",null)).on("mousemove",(e)=>{{
                const x0 = x.invert(d3.pointer(e)[0]), i = d3.bisector(d=>d[tk]).left(data,x0,1), d = x0-data[i-1][tk]>data[i][tk]-x0?data[i]:data[i-1];
                focus.select("#fL").attr("x1",x(d[tk])).attr("x2",x(d[tk])); fD.attr("cx",x(d[tk])).attr("cy",yD(d.depth)); fT.attr("cx",x(d[tk])).attr("cy",yT(d.temp));
                const m = Math.floor(d.sec/60).toString().padStart(2,'0'), s = Math.floor(d.sec%60).toString().padStart(2,'0');
                d3.select("#data-display").html(`<span style="color:#333;font-weight:bold;font-family:monospace;font-size:18px;">${{m}}:${{s}}</span> | Depth: <span style="color:steelblue;font-weight:bold;">${{d.depth.toFixed(1)}}m</span> | Temp: <span style="color:indianred;font-weight:bold;">${{d.temp.toFixed(1)}}&deg;C</span>`);
            }});
    </script></body></html>
    """

# --- 4. 側邊欄 ---
def on_mode_change():
    m = st.session_state.nav_mode
    if m in db_index:
        d = sorted(list(db_index[m].keys()), reverse=True)[0]
        st.session_state.nav_date, st.session_state.nav_time = d, sorted(list(db_index[m][d].keys()))[0]

def on_date_change():
    m, d = st.session_state.nav_mode, st.session_state.nav_date
    if m in db_index and d in db_index[m]:
        st.session_state.nav_time = sorted(list(db_index[m][d].keys()))[0]

data_res = load_data()
global_stats, db_index, flat_logs = data_res if data_res else (None, None, None)

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
    st.sidebar.markdown("**• 下潛時間**")
    
    # 🚀 修復 InvalidHeightError: 如果數量少，完全不傳入 height 參數
    t_cont = st.sidebar.container(height=200, border=False)

    with t_cont:
        st.markdown('<div class="time-mask"></div>', unsafe_allow_html=True)
        for t in available_times:
            b_type = "primary" if st.session_state.nav_time == t else "secondary"
            if st.button(f"⏱️ {t}", key=f"s_{t}", type=b_type, use_container_width=True): 
                st.session_state.nav_time = t; st.rerun()
    
    js = f"""<script>setTimeout(()=>{{
        const target = "⏱️ {st.session_state.nav_time}";
        const btns = window.parent.document.querySelectorAll('[data-testid="stSidebar"] button');
        for (let b of btns) {{ if (b.innerText.includes(target)) {{ b.scrollIntoView({{behavior:'smooth', block:'center'}}); break; }} }}
        const m = window.parent.document.querySelector('.time-mask');
        if(m){{let c=m.closest('div[data-testid="stVerticalBlock"]'); if(c){{c.style.WebkitMaskImage='linear-gradient(to bottom, transparent 0%, black 15%, black 88%, transparent 100%)'; c.style.paddingBottom='20px';}}}}
    }},150);</script>"""
    st.sidebar.html(js,unsafe_allow_javascript=True)

st.sidebar.header("🔄 資料同步")
sync_clicked = st.sidebar.button("從手錶連線並同步", use_container_width=True, type="primary")
msg_area = st.sidebar.container()
if "sync_msg" in st.session_state:
    msg_area.success(st.session_state.sync_msg); del st.session_state.sync_msg
if sync_clicked:
    with msg_area:
        stat = st.empty()
        ok, res = asyncio.run(sync_from_watch(stat))
        if ok: st.session_state.sync_msg = f"✅ 完成！雲端新增 {res} 筆。"; st.cache_data.clear(); st.rerun()
        else: stat.empty(); st.error(res)

# --- 5. 主畫面展現 ---
if global_stats and "nav_mode" in st.session_state:
    st.title("🤿 潛水生涯大數據分析", anchor=False)
    sc, fc = st.columns(2)
    with sc:
        st.markdown("#### 🐠 Scuba Diving")
        c1, c2, c3 = st.columns(3)
        c1.metric("次數", f"{global_stats['Scuba Diving']['count']} 支"); c2.metric("總時長", format_duration(global_stats['Scuba Diving']['total_sec'])); c3.metric("最大深度", f"{global_stats['Scuba Diving']['max_depth']:.1f} m")
    with fc:
        st.markdown("#### 🧜‍♀️ Free Diving")
        c1, c2, c3 = st.columns(3)
        c1.metric("次數", f"{global_stats['Free Diving']['count']} 次"); c2.metric("總時長", format_duration(global_stats['Free Diving']['total_sec'])); c3.metric("最大深度", f"{global_stats['Free Diving']['max_depth']:.1f} m")
    st.divider()

    curr_list = flat_logs[st.session_state.nav_mode]
    idx = next((i for i, l in enumerate(curr_list) if l["date"] == st.session_state.nav_date and l["time"] == st.session_state.nav_time), 0)
    nav1, nav2, nav3 = st.columns([1, 8, 1])
    with nav1:
        if idx > 0:
            p = curr_list[idx-1]
            st.button("◀", key="prev", type="tertiary", use_container_width=True, on_click=lambda d=p["date"], t=p["time"]: st.session_state.update({"nav_date": d, "nav_time": t}))
    with nav2:
        e = db_index[st.session_state.nav_mode][st.session_state.nav_date][st.session_state.nav_time]
        st.markdown(f"<h3 style='text-align: center; margin-top: 0;'>📊 {st.session_state.nav_mode} #{e['num']} | {st.session_state.nav_date} {st.session_state.nav_time}</h3>", unsafe_allow_html=True)
    with nav3:
        if idx < len(curr_list)-1:
            n = curr_list[idx+1]
            st.button("▶", key="next", type="tertiary", use_container_width=True, on_click=lambda d=n["date"], t=n["time"]: st.session_state.update({"nav_date": d, "nav_time": t}))

    log_entry = curr_list[idx]["cloud_data"]
    info = parse_header(log_entry["header_hex"])
    raw_b = b"".join(bytes.fromhex(h) for h in log_entry["profile_hex_list"])[2:]
    p_data, sec, marker, itv = [], 0.0, None, (0.5 if "Free" in st.session_state.nav_mode else info["sampling_rate"])
    while len(raw_b) >= 6:
        chunk = raw_b[:6]; raw_b = raw_b[1:]
        if marker is None or chunk[0] in [marker, (marker-1)%256]:
            d_r, t_r = int.from_bytes(chunk[1:3],"little"), int.from_bytes(chunk[3:5],"little")
            if 100 <= t_r <= 450 and 900 <= d_r <= 16000:
                marker = chunk[0]; p_data.append({"sec":sec, "min":round(sec/60,1), "depth":(d_r-1000)/100.0, "temp":t_r/10.0})
                sec += itv; raw_b = raw_b[5:]
    
    if p_data:
        depths = [d['depth'] for d in p_data]
        dive_time, max_d, min_t = p_data[-1]['sec'], max(depths), min(d['temp'] for d in p_data)
        if "Free" in st.session_state.nav_mode:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("潛水時長", format_duration(dive_time)); m2.metric("最大深度", f"{max_d:.1f} m"); m3.metric("下潛時間", f"{int(p_data[depths.index(max_d)]['sec'])} 秒"); m4.metric("最低溫度", f"{min_t:.1f} °C")
        else:
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("潛水時長", format_duration(dive_time)); m2.metric("最大深度", f"{max_d:.1f} m"); m3.metric("平均深度", f"{sum(depths)/len(depths):.1f} m"); m4.metric("最低溫度", f"{min_t:.1f} °C"); m5.metric("Max CNS", f"{info['cns_max']} %")
        b64 = base64.b64encode(d3_plot(p_data, "Free" in st.session_state.nav_mode).encode('utf-8')).decode('utf-8')
        st.iframe(f"data:text/html;base64,{b64}", height=380)
else:
    st.info("👋 歡迎！請點擊同步或檢查雲端 Firebase 數據。")
