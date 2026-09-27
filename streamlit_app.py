from __future__ import annotations

from pathlib import Path
from io import StringIO
from datetime import datetime
from zoneinfo import ZoneInfo
import csv
import html
import os
import re
import time
import unicodedata

import pandas as pd
import pydeck as pdk
import requests
import streamlit as st

st.set_page_config(
    page_title="Painel Geográfico de Nodes — Esteio + Sapucaia",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
BASE_FILE = ROOT / "base_nodes_esteio_sapucaia.csv"
LOCAL_CSV = DATA_DIR / "ESTEIO_SAPUCAIA.csv"
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
REFRESH_MINUTES = 60
CRITICAL_MAX_SCORE = 20
LIGHT_MAP_STYLE = "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json"


def _secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default) or default).strip()
    except Exception:
        return str(os.getenv(name, default) or default).strip()


# V1 funciona com o CSV incluído no pacote. Para atualização pelo Drive,
# basta configurar DRIVE_FOLDER_ID (e manter o nome ESTEIO_SAPUCAIA.csv).
DRIVE_FOLDER_ID = _secret("DRIVE_FOLDER_ID", "")
DRIVE_FILE_ID = _secret("DRIVE_FILE_ID", "")
DRIVE_FILE_NAME = _secret("DRIVE_FILE_NAME", "ESTEIO_SAPUCAIA.csv")


def norm_txt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip().upper()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)


def norm_col(v) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", norm_txt(v)).strip("_")


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def fmt_int(v) -> str:
    try:
        return f"{int(v):,}".replace(",", ".")
    except Exception:
        return "—"


@st.cache_data(show_spinner=False)
def load_base() -> pd.DataFrame:
    d = pd.read_csv(BASE_FILE)
    text_cols = ["Node", "Cidade", "Regiao", "Bairro", "HV", "Endereco", "Precisao_Bairro", "Fonte_Bairro", "Observacao"]
    for c in text_cols:
        if c not in d.columns:
            d[c] = ""
        d[c] = d[c].fillna("").astype(str)
    for c in ["Latitude", "Longitude"]:
        d[c] = pd.to_numeric(d.get(c), errors="coerce")
    d["Node"] = d["Node"].map(norm_txt)
    return d


@st.cache_data(show_spinner=False)
def read_csv_bytes(data: bytes) -> pd.DataFrame:
    last_error = None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = data.decode(enc, errors="strict")
            df = pd.read_csv(StringIO(text), sep=None, engine="python")
            if df.shape[1] == 1 and "," in str(df.columns[0]):
                outer = list(csv.reader(StringIO(text)))
                inner = [next(csv.reader([r[0]])) if len(r) == 1 else r for r in outer]
                if inner and len(inner[0]) > 1:
                    width = len(inner[0])
                    rows = [r for r in inner[1:] if len(r) == width]
                    return pd.DataFrame(rows, columns=inner[0])
            return df
        except Exception as e:
            last_error = e
    raise ValueError(f"Não foi possível abrir o CSV: {last_error}")


@st.cache_data(ttl=60, show_spinner=False)
def fetch_drive_csv(cache_minute: int):
    if not DRIVE_FOLDER_ID and not DRIVE_FILE_ID:
        raise FileNotFoundError("Drive ainda não configurado na V1.")
    headers = {
        "User-Agent": "Mozilla/5.0 Painel-Nodes-Esteio-Sapucaia/1.0",
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
    }
    file_id = DRIVE_FILE_ID
    if not file_id and DRIVE_FOLDER_ID:
        folder_url = f"https://drive.google.com/drive/folders/{DRIVE_FOLDER_ID}?usp=sharing&_cb={cache_minute}"
        fr = requests.get(folder_url, headers=headers, timeout=25)
        fr.raise_for_status()
        page = fr.text
        pos = page.lower().find(DRIVE_FILE_NAME.lower())
        if pos >= 0:
            window = page[max(0, pos - 3500): pos + 3500]
            candidates = re.findall(r"([A-Za-z0-9_-]{20,})", window)
            for cand in candidates:
                u = f"https://drive.google.com/uc?export=download&id={cand}&_cb={cache_minute}"
                rr = requests.get(u, headers=headers, timeout=20, allow_redirects=True)
                head = rr.content[:2500]
                if rr.ok and b"Node" in head and (b"Pontua" in head or b"Pontua" in rr.content[:5000]):
                    file_id = cand
                    break
    if not file_id:
        raise FileNotFoundError(f"{DRIVE_FILE_NAME} não localizado na pasta pública do Drive.")
    url = f"https://drive.google.com/uc?export=download&id={file_id}&_cb={cache_minute}"
    r = requests.get(url, headers=headers, timeout=35, allow_redirects=True)
    r.raise_for_status()
    if b"Node" not in r.content[:2500]:
        raise ValueError("O arquivo do Drive não parece ser a extração XPT esperada.")
    return r.content, r.headers.get("Last-Modified", ""), file_id


def schema(df: pd.DataFrame):
    by = {norm_col(c): c for c in df.columns}
    if "NODE" not in by or "PONTUACAO" not in by:
        return None
    return {
        "NODE": by["NODE"],
        "PONTUACAO": by["PONTUACAO"],
        "IMPACTADO": by.get("IMPACTADO"),
        "ESTRESSADO": by.get("ESTRESSADO"),
        "TOTAL": by.get("TOTAL"),
    }


def split_node_port(v):
    """Normaliza as exceções conhecidas da operação Esteio/Sapucaia."""
    s = norm_txt(v)
    # XPT: CPCxx / Topologia: CCPxx
    m = re.match(r"^(CPC0[123])-([1-4])$", s)
    if m:
        return m.group(1).replace("CPC", "CCP"), int(m.group(2))
    # PIR04 está dividido no XPT em dois nomes, mas é um node lógico na topologia.
    m = re.match(r"^PIR04A([12])-([1-4])$", s)
    if m:
        return "PIR04", int(m.group(1))
    m = re.match(r"^(.+?)-([1-4])$", s)
    if m:
        return m.group(1).strip(), int(m.group(2))
    m = re.match(r"^(.+?)[\s_/]+([1-4])$", s)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return s, 1


def port_state(score):
    if score is None or pd.isna(score):
        return ""
    try:
        v = float(score)
    except Exception:
        return ""
    if v == 0:
        return "OFF"
    if 0 < v <= CRITICAL_MAX_SCORE:
        return "CRÍTICA"
    return "ONLINE"


def build_status(xdf: pd.DataFrame) -> pd.DataFrame:
    sch = schema(xdf)
    if not sch:
        return pd.DataFrame()
    d = xdf.copy()
    parsed = d[sch["NODE"]].map(split_node_port)
    d["_node"] = [x[0] for x in parsed]
    d["_port"] = [x[1] for x in parsed]
    d["_raw"] = d[sch["NODE"]].astype(str)
    d["_score"] = pd.to_numeric(d[sch["PONTUACAO"]], errors="coerce")
    for key, target in [("IMPACTADO", "_impactado"), ("ESTRESSADO", "_estressado"), ("TOTAL", "_total")]:
        col = sch.get(key)
        d[target] = pd.to_numeric(d[col], errors="coerce").fillna(0) if col else 0

    rows = []
    for node, g in d.groupby("_node"):
        ports = sorted(set(int(x) for x in g["_port"].dropna().tolist() if int(x) in (1, 2, 3, 4)))
        scores, states = {}, {}
        for p in ports:
            vals = g.loc[g["_port"] == p, "_score"].dropna()
            score = float(vals.min()) if len(vals) else None
            scores[p] = score
            states[p] = port_state(score)
        total_ports = len(ports)
        off = sum(states[p] == "OFF" for p in ports)
        critical = sum(states[p] == "CRÍTICA" for p in ports)
        if total_ports and off == total_ports:
            status = "SS TOTAL"
        elif off > 0:
            status = "SS PARCIAL"
        else:
            status = "ONLINE"
        details = []
        row = {
            "Node": norm_txt(node),
            "Status": status,
            "Total_portas": total_ports,
            "Portas_OFF": int(off),
            "Portas_Criticas": int(critical),
            "Impactado": int(g["_impactado"].sum()),
            "Estressado": int(g["_estressado"].sum()),
            "Total_XPT": int(g["_total"].sum()),
            "Raw_Nodes": ", ".join(sorted(g["_raw"].unique().tolist())),
        }
        for p in ports:
            sc = scores.get(p)
            stt = states.get(p, "")
            sc_txt = "—" if sc is None or pd.isna(sc) else str(int(sc) if float(sc).is_integer() else round(float(sc), 1))
            details.append(f"P{p}: {stt} ({sc_txt})")
        row["Portas_popup"] = " | ".join(details) if details else "Sem leitura de porta"
        rows.append(row)
    return pd.DataFrame(rows)


def color_for(status):
    return {
        "ONLINE": [16, 165, 90, 235],
        "SS PARCIAL": [245, 183, 0, 240],
        "SS TOTAL": [239, 51, 64, 245],
        "SEM COLETA": [145, 155, 170, 220],
    }.get(status, [145, 155, 170, 220])


def status_order(status):
    return {"SS TOTAL": 4, "SS PARCIAL": 3, "SEM COLETA": 2, "ONLINE": 1}.get(status, 0)


def impact_summary(df: pd.DataFrame, col: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    g = df.groupby(col, dropna=False).agg(
        Nodes=("Node", "nunique"),
        Impactado=("Impactado", "sum"),
        Portas_OFF=("Portas_OFF", "sum"),
        SS_Total=("Status", lambda s: int((s == "SS TOTAL").sum())),
        SS_Parcial=("Status", lambda s: int((s == "SS PARCIAL").sum())),
    ).reset_index()
    return g.sort_values(["Impactado", "Portas_OFF", "Nodes"], ascending=[False, False, False]).reset_index(drop=True)


# ---------- visual ----------
st.markdown(
    """
<style>
:root { --navy:#0b2443; --blue:#1677ff; --bg:#f4f7fb; --card:#fff; --text:#0b1736; --muted:#6b7892; --line:#e4eaf2; --yellow:#f5b700; --red:#ef3340; --green:#10a55a; }
html, body, [class*="css"] { font-family: Inter, "Segoe UI", Arial, sans-serif; }
[data-testid="stAppViewContainer"] { background:var(--bg); color:var(--text); }
[data-testid="stHeader"] { background:transparent; }
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] { display:none !important; }
#MainMenu, footer { visibility:hidden; }
.block-container { padding-top:.7rem; padding-bottom:1.5rem; max-width:1900px; }
.topbar { background:#0b2443; color:white; border-radius:15px; padding:18px 22px; margin-bottom:13px; box-shadow:0 8px 24px rgba(11,36,67,.12); }
.topbar h1 { margin:0; color:white; font-size:28px; line-height:1.08; }
.topbar .sub { color:#cbd8ea; margin-top:5px; font-size:14px; }
.topbar .meta { color:#9eb1cb; margin-top:6px; font-size:12px; }
.kpi-grid { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; margin:10px 0 12px; }
.kpi { background:white; border:1px solid #e4eaf2; border-radius:13px; padding:13px 15px; min-height:91px; box-shadow:0 3px 12px rgba(11,36,67,.05); }
.kpi .label { color:#6b7892; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.03em; }
.kpi .value { color:#0b1736; font-size:28px; font-weight:800; margin-top:5px; }
.kpi .sub { color:#6b7892; font-size:11px; margin-top:3px; }
.kpi.online { border-top:4px solid #10a55a; }.kpi.partial { border-top:4px solid #f5b700; }.kpi.total { border-top:4px solid #ef3340; }.kpi.blue { border-top:4px solid #1677ff; }
.focus { background:white; border:1px solid #dfe8f5; border-left:5px solid #1677ff; border-radius:12px; padding:12px 15px; margin:8px 0 10px; }
.small { color:#6b7892; font-size:12px; }
.note { background:#eef6ff; border:1px solid #d3e6ff; border-left:4px solid #1677ff; border-radius:11px; padding:10px 13px; color:#294664; margin:8px 0 12px; font-size:12px; }
.crisis-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin:2px 0 14px; }
.crisis { background:#fff; border:1px solid #e4eaf2; border-radius:13px; padding:12px 14px; }
.crisis .t { font-size:11px; color:#6b7892; font-weight:700; text-transform:uppercase; }.crisis .v { font-size:19px; font-weight:800; margin-top:4px; }.crisis .s { font-size:11px; color:#6b7892; margin-top:3px; }
[data-testid="stDataFrame"] { background:white; border-radius:12px; }
@media (max-width:1100px){ .kpi-grid{grid-template-columns:repeat(3,minmax(0,1fr));}.crisis-grid{grid-template-columns:1fr;} }
@media (max-width:700px){ .kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr));}.topbar h1{font-size:21px;} }
</style>
""",
    unsafe_allow_html=True,
)
st.markdown(f"<meta http-equiv='refresh' content='{REFRESH_MINUTES * 60}'>", unsafe_allow_html=True)

base = load_base()
xraw = None
source_file_name = ""
source_updated = None
source_mode = ""
source_error = ""

head_left, head_right = st.columns([8, 1.2])
with head_right:
    if st.button("🔄 Atualizar", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# Upload manual é opcional e útil para calibração da V1.
with st.expander("🧪 Testar uma nova extração XPT nesta sessão", expanded=False):
    uploaded = st.file_uploader("CSV do XPERTrack", type=["csv"], accept_multiple_files=False)
    st.caption("O upload é apenas para teste e não altera os arquivos do GitHub. A versão definitiva pode ler a coleta do Drive.")

if uploaded is not None:
    try:
        xraw = read_csv_bytes(uploaded.getvalue())
        source_file_name = uploaded.name
        source_mode = "upload manual"
        source_updated = datetime.now(LOCAL_TZ)
    except Exception as e:
        source_error = str(e)

if xraw is None and (DRIVE_FOLDER_ID or DRIVE_FILE_ID):
    try:
        raw, last_mod, _drive_file_id = fetch_drive_csv(int(time.time() // 60))
        xraw = read_csv_bytes(raw)
        source_file_name = DRIVE_FILE_NAME
        source_mode = "Google Drive"
        if last_mod:
            try:
                source_updated = pd.to_datetime(last_mod, utc=True).tz_convert(LOCAL_TZ).to_pydatetime()
            except Exception:
                pass
    except Exception as e:
        source_error = str(e)

if xraw is None and LOCAL_CSV.exists():
    try:
        xraw = read_csv_bytes(LOCAL_CSV.read_bytes())
        source_file_name = LOCAL_CSV.name
        source_mode = "coleta incluída na V1"
        source_updated = datetime.fromtimestamp(LOCAL_CSV.stat().st_mtime, tz=LOCAL_TZ)
    except Exception as e:
        source_error = (source_error + " | " if source_error else "") + str(e)

status_df = build_status(xraw) if xraw is not None and not xraw.empty else pd.DataFrame()
logical = base.merge(status_df, on="Node", how="left")
logical["Status"] = logical["Status"].fillna("SEM COLETA")
for c in ["Portas_OFF", "Portas_Criticas", "Total_portas", "Impactado", "Estressado", "Total_XPT"]:
    logical[c] = pd.to_numeric(logical.get(c, 0), errors="coerce").fillna(0).astype(int)
for c in ["Portas_popup", "Raw_Nodes"]:
    logical[c] = logical.get(c, pd.Series(index=logical.index, dtype=str)).fillna("")
logical["color"] = logical["Status"].map(color_for)

updated_txt = source_updated.strftime("%d/%m/%Y %H:%M") if source_updated else "horário não informado"
with head_left:
    st.markdown(
        f"""<div class='topbar'><h1>Painel Geográfico de Nodes — Esteio + Sapucaia</h1>
        <div class='sub'>XPERTrack • foco operacional por cidade, região/HV e bairro</div>
        <div class='meta'>Fonte: {esc(source_mode or 'não disponível')} • {esc(source_file_name or DRIVE_FILE_NAME)} • {esc(updated_txt)}</div></div>""",
        unsafe_allow_html=True,
    )

if status_df.empty:
    st.error("A coleta do XPERTrack não foi carregada ou o CSV não contém Node + Pontuação.")
elif source_error and source_mode != "Google Drive":
    st.caption("Drive não configurado/disponível nesta V1; usando a coleta local de teste.")

# ---- filtros ----
f1, f2, f3, f4 = st.columns([1.4, 1.2, 1.8, 1.3])
with f1:
    cidade = st.selectbox("Cidade", ["TODAS"] + sorted(logical["Cidade"].unique().tolist()))
city_df = logical if cidade == "TODAS" else logical[logical["Cidade"] == cidade]
with f2:
    regioes = sorted([x for x in city_df["Regiao"].dropna().unique().tolist() if str(x).strip()])
    regiao = st.selectbox("Região operacional / HV", ["TODAS"] + regioes)
reg_df = city_df if regiao == "TODAS" else city_df[city_df["Regiao"] == regiao]
with f3:
    bairros = sorted([x for x in reg_df["Bairro"].dropna().unique().tolist() if str(x).strip()])
    bairro = st.selectbox("Bairro", ["TODOS"] + bairros)
bairro_df = reg_df if bairro == "TODOS" else reg_df[reg_df["Bairro"] == bairro]
with f4:
    statuses = ["TODOS", "ONLINE", "SS PARCIAL", "SS TOTAL", "SEM COLETA"]
    status_filter = st.selectbox("Status", statuses)
filtered = bairro_df if status_filter == "TODOS" else bairro_df[bairro_df["Status"] == status_filter]

# KPIs seguem os filtros geográficos/status escolhidos.
monitored = int(len(filtered))
online = int((filtered["Status"] == "ONLINE").sum())
partial = int((filtered["Status"] == "SS PARCIAL").sum())
total_off = int((filtered["Status"] == "SS TOTAL").sum())
ports_off = int(filtered["Portas_OFF"].sum())
impactado = int(filtered["Impactado"].sum())

cards = [
    ("Nodes", monitored, "nodes lógicos na visão", "blue"),
    ("Online", online, "sem porta zerada", "online"),
    ("SS Parcial", partial, "1+ porta OFF", "partial"),
    ("SS Total", total_off, "todas as portas OFF", "total"),
    ("Portas OFF", ports_off, "Pontuação = 0", "blue"),
    ("Impactado XPT", impactado, "soma do campo Impactado", "blue"),
]
st.markdown("<div class='kpi-grid'>" + "".join(
    f"<div class='kpi {cl}'><div class='label'>{lab}</div><div class='value'>{fmt_int(val)}</div><div class='sub'>{sub}</div></div>"
    for lab, val, sub, cl in cards
) + "</div>", unsafe_allow_html=True)

# Visão de crise pelo próprio campo Impactado do XPT.
def top_label(df, col):
    s = impact_summary(df, col)
    if s.empty:
        return "—", 0
    r = s.iloc[0]
    return str(r[col]), int(r["Impactado"])

top_city, top_city_imp = top_label(filtered, "Cidade")
top_reg, top_reg_imp = top_label(filtered, "Regiao")
top_bairro, top_bairro_imp = top_label(filtered, "Bairro")
st.markdown(
    "<div class='crisis-grid'>"
    f"<div class='crisis'><div class='t'>Cidade com maior Impactado</div><div class='v'>{esc(top_city)}</div><div class='s'>{fmt_int(top_city_imp)} no campo Impactado</div></div>"
    f"<div class='crisis'><div class='t'>Região/HV com maior Impactado</div><div class='v'>{esc(top_reg)}</div><div class='s'>{fmt_int(top_reg_imp)} no campo Impactado</div></div>"
    f"<div class='crisis'><div class='t'>Bairro com maior Impactado</div><div class='v'>{esc(top_bairro)}</div><div class='s'>{fmt_int(top_bairro_imp)} no campo Impactado</div></div>"
    "</div>", unsafe_allow_html=True
)

st.markdown("<div class='note'><b>Mapa operacional:</b> os pontos são aproximados e servem para leitura de concentração por bairro/região. Não usar como coordenada técnica para despacho endereço-a-endereço.</div>", unsafe_allow_html=True)

# Busca centraliza e não muda KPIs.
search_col, info_col = st.columns([2.1, 4.9])
with search_col:
    nodes_search = sorted(filtered["Node"].dropna().unique().tolist())
    chosen = st.selectbox("🔎 Localizar node", [""] + nodes_search, format_func=lambda x: "Digite ou selecione o node..." if not x else x)
with info_col:
    if chosen:
        rr = filtered[filtered["Node"] == chosen]
        if not rr.empty:
            r = rr.iloc[0]
            st.markdown(
                f"<div class='focus'><b>📍 {esc(r['Node'])}</b> — {esc(r['Status'])}<br>"
                f"<span class='small'>{esc(r['Cidade'])} • {esc(r['Regiao'])} • {esc(r['Bairro'])} • {esc(r['Endereco'])}</span><br>"
                f"<span class='small'>{esc(r['Portas_popup'] or 'Sem detalhe de porta')} • Impactado: {fmt_int(r['Impactado'])}</span></div>",
                unsafe_allow_html=True,
            )

mapdf = filtered[filtered["Latitude"].notna() & filtered["Longitude"].notna()].copy()
focus = mapdf[mapdf["Node"] == chosen].copy() if chosen else pd.DataFrame()
if mapdf.empty:
    st.warning("Nenhum node disponível para o mapa com os filtros escolhidos.")
else:
    if not focus.empty:
        center_lat, center_lon, zoom = float(focus.iloc[0]["Latitude"]), float(focus.iloc[0]["Longitude"]), 14.0
    else:
        center_lat, center_lon = float(mapdf["Latitude"].mean()), float(mapdf["Longitude"].mean())
        zoom = 11.7 if cidade == "TODAS" else 12.4
    layers = [pdk.Layer(
        "ScatterplotLayer", data=mapdf, get_position="[Longitude, Latitude]", get_radius=55,
        radius_min_pixels=6, radius_max_pixels=15, get_fill_color="color",
        get_line_color=[255,255,255,230], line_width_min_pixels=1.8, stroked=True,
        pickable=True, auto_highlight=True,
    )]
    if not focus.empty:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=focus, get_position="[Longitude, Latitude]", get_radius=95,
            radius_min_pixels=12, radius_max_pixels=22, filled=False, stroked=True,
            get_line_color=[22,119,255,255], line_width_min_pixels=4, pickable=False,
        ))
    tooltip = {
        "html": "<div style='font-size:13px;line-height:1.5;min-width:270px'>"
                "<div style='font-size:18px;font-weight:800;margin-bottom:5px'>{Node}</div>"
                "<div><b>Status:</b> {Status}</div><div><b>Cidade:</b> {Cidade}</div>"
                "<div><b>Região/HV:</b> {Regiao}</div><div><b>Bairro:</b> {Bairro}</div>"
                "<div><b>Portas OFF:</b> {Portas_OFF}/{Total_portas}</div><div><b>Impactado XPT:</b> {Impactado}</div>"
                "<div style='border-top:1px solid rgba(255,255,255,.2);margin:7px 0 5px'></div>"
                "<div>{Portas_popup}</div><div style='margin-top:5px'><b>Endereço base:</b> {Endereco}</div>"
                "</div>",
        "style": {"backgroundColor":"rgba(9,26,51,.97)","color":"white","borderRadius":"10px","padding":"10px 12px"},
    }
    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=zoom, pitch=0),
        tooltip=tooltip, map_style=LIGHT_MAP_STYLE,
    )
    st.pydeck_chart(deck, use_container_width=True, height=560)

# ---- rankings de crise ----
st.markdown("### 📊 Concentração do impacto")
r1, r2 = st.columns(2)
with r1:
    st.markdown("#### Bairros mais impactados")
    bsum = impact_summary(filtered, "Bairro")
    if bsum.empty:
        st.info("Sem dados.")
    else:
        show = bsum.head(12).copy()
        show.columns = ["Bairro", "Nodes", "Impactado XPT", "Portas OFF", "SS Total", "SS Parcial"]
        st.dataframe(show, use_container_width=True, hide_index=True)
with r2:
    st.markdown("#### Regiões/HV mais impactadas")
    rsum = impact_summary(filtered, "Regiao")
    if rsum.empty:
        st.info("Sem dados.")
    else:
        show = rsum.head(12).copy()
        show.columns = ["Região/HV", "Nodes", "Impactado XPT", "Portas OFF", "SS Total", "SS Parcial"]
        st.dataframe(show, use_container_width=True, hide_index=True)

c1, c2 = st.columns(2)
with c1:
    st.markdown("### 🔴 Sem sinal")
    crit = filtered[filtered["Portas_OFF"] > 0].copy()
    if crit.empty:
        st.success("Nenhuma porta OFF na leitura atual para os filtros selecionados.")
    else:
        crit["_sev"] = crit["Status"].map(status_order)
        crit = crit.sort_values(["_sev", "Portas_OFF", "Impactado"], ascending=[False, False, False])
        st.dataframe(crit[["Node","Cidade","Regiao","Bairro","Status","Portas_OFF","Total_portas","Impactado"]], use_container_width=True, hide_index=True)
with c2:
    st.markdown("### 🎯 Nodes com maior Impactado XPT")
    topn = filtered.sort_values(["Impactado","Portas_OFF"], ascending=[False,False]).head(12)
    st.dataframe(topn[["Node","Cidade","Regiao","Bairro","Status","Impactado","Estressado","Total_XPT"]], use_container_width=True, hide_index=True)

pending = logical[logical["Precisao_Bairro"].astype(str).str.contains("VALIDAR|SEM TOPOLOGIA", case=False, regex=True)].copy()
with st.expander(f"🧭 Base V1 — bairros/regiões para validar ({len(pending)})", expanded=False):
    st.caption("Esses pontos foram mantidos no mapa, mas a classificação de bairro ainda é uma aproximação de V1. Corrigimos após comparar visualmente com a operação.")
    st.dataframe(pending[["Node","Cidade","Regiao","Bairro","Endereco","Precisao_Bairro"]], use_container_width=True, hide_index=True)

st.caption("Legenda principal: 🟢 Online • 🟡 SS Parcial • 🔴 SS Total • cinza = sem coleta. Porta crítica 1–20 fica nos detalhes e não cria uma quarta cor principal.")
