import io
import os
import json
import threading
from datetime import datetime, timezone, timedelta

import requests
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ─────────────────────── CONFIG ─────────────────────────────────
BACKEND_HOST  = "49.200.100.22"
PORT_A        = 6007
PORT_B        = 6008
LABEL_A       = "Port 6007"
LABEL_B       = "Port 6008"
TIMEOUT_SEC   = 240
GRACE_SEC     = 3

DRIVE_FOLDER  = "1D2C_Cq034E2N68F3HtDS3N5yuF7gzFKP"
SHEET_ID      = "1HmP5c0xR3CuvkDakip4J5pdzB6hssy-XRuoOu6iBxNI"
SHEET_TAB     = "Sheet1"
DATA_START_ROW = 3

SAVE_DIR = os.path.expanduser("~/Streamlit/Audio/Kannada")
os.makedirs(SAVE_DIR, exist_ok=True)

IST = timezone(timedelta(hours=5, minutes=30))
COL_LETTERS = ["A","B","C","D","E","F","G","H","I","J"]
PORT_COL_OFFSET = {PORT_A: 1, PORT_B: 5}  # B=1, F=5

# ─────────────────────── GOOGLE CLIENTS ─────────────────────────
@st.cache_resource
def _google_clients():
    """Build Drive + Sheets clients from Streamlit secrets. Cached across reruns."""
    key_dict = dict(st.secrets["gcp_service_account"])
    creds = service_account.Credentials.from_service_account_info(
        key_dict,
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )
    drive  = build("drive",  "v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return drive, sheets


# ─────────────────────── DRIVE UPLOAD ───────────────────────────
def upload_to_drive(audio_bytes: bytes, filename: str) -> str:
    """Upload WAV bytes to Drive from Streamlit Cloud. Returns view URL."""
    drive, _ = _google_clients()

    media = MediaIoBaseUpload(
        io.BytesIO(audio_bytes), mimetype="audio/wav", resumable=False
    )
    uploaded = (
        drive.files()
        .create(
            body={"name": filename, "parents": [DRIVE_FOLDER]},
            media_body=media,
            fields="id",
        )
        .execute()
    )
    file_id = uploaded["id"]

    drive.permissions().create(
        fileId=file_id,
        body={"role": "reader", "type": "anyone"},
    ).execute()

    return f"https://drive.google.com/file/d/{file_id}/view"


# ─────────────────────── SHEETS HELPERS ─────────────────────────
def _all_filenames(sheets) -> list:
    result = (
        sheets.spreadsheets().values()
        .get(spreadsheetId=SHEET_ID,
             range=f"{SHEET_TAB}!A{DATA_START_ROW}:A")
        .execute()
    )
    names = []
    for r in result.get("values", []):
        cell = r[0] if r else ""
        if cell.startswith("=HYPERLINK"):
            try:
                cell = cell.split('"')[3]
            except IndexError:
                pass
        names.append(cell)
    return names


def _find_or_create_row(sheets, filename: str, drive_url: str) -> int:
    names = _all_filenames(sheets)
    if filename in names:
        return DATA_START_ROW + names.index(filename)

    row_idx   = DATA_START_ROW + len(names)
    timestamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    hyperlink = f'=HYPERLINK("{drive_url}","{filename}")'

    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB}!A{row_idx}:J{row_idx}",
        valueInputOption="USER_ENTERED",
        body={"values": [[hyperlink,"","","","","","","","",timestamp]]},
    ).execute()
    return row_idx


def _write_port_columns(sheets, row_idx, port, raw_k, corrected, english, rtt):
    offset  = PORT_COL_OFFSET.get(port)
    if offset is None:
        return
    c_start = COL_LETTERS[offset]
    c_end   = COL_LETTERS[offset + 3]
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB}!{c_start}{row_idx}:{c_end}{row_idx}",
        valueInputOption="USER_ENTERED",
        body={"values": [[raw_k, corrected, english, round(rtt, 3)]]},
    ).execute()


def log_to_sheet(audio_bytes, filename, port, raw_k, corrected, english, rtt):
    """Upload to Drive + write Sheet row. Called in background thread."""
    try:
        drive_url = upload_to_drive(audio_bytes, filename)
        _, sheets = _google_clients()
        row_idx   = _find_or_create_row(sheets, filename, drive_url)
        _write_port_columns(sheets, row_idx, port, raw_k, corrected, english, rtt)
    except Exception as exc:
        st.session_state.setdefault("log_errors", []).append(
            f"[{port}] {exc}"
        )


# ─────────────────────── HELPERS ────────────────────────────────
def make_filename() -> str:
    now = datetime.now(IST)
    ms  = now.microsecond // 1000
    return (
        f"streamlit_"
        f"{now.second:02d}{ms:03d}_"
        f"{now.minute:02d}_"
        f"{now.hour:02d}_"
        f"{now.day:02d}_"
        f"{now.month:02d}_"
        f"{now.year}.wav"
    )


def parse_response(data: dict) -> dict:
    entry = data
    if "results" in data and isinstance(data["results"], list) and data["results"]:
        entry = data["results"][0]
    return {
        "raw_kannada":         entry.get("raw_hindi") or entry.get("raw_transcription") or entry.get("raw_kannada") or "N/A",
        "corrected_kannada":   entry.get("corrected_hindi") or entry.get("corrected_kannada") or "N/A",
        "english_translation": entry.get("english_translation") or entry.get("translation") or "N/A",
        "audio_duration":      entry.get("audio_duration_seconds"),
        "file":                entry.get("file", "N/A"),
        "slowed_applied":      entry.get("slowed_applied"),
        "speed_factor":        entry.get("speed_factor"),
        "_raw":                data,
    }


def call_backend(port, filename, audio_bytes):
    url = f"http://{BACKEND_HOST}:{port}/convertSpeechToText"
    try:
        t0   = __import__("time").perf_counter()
        resp = requests.post(
            url,
            files={"file": (filename, io.BytesIO(audio_bytes), "audio/wav")},
            timeout=TIMEOUT_SEC,
        )
        rtt = round(__import__("time").perf_counter() - t0, 3)
        if resp.status_code != 200:
            return None, rtt, f"HTTP {resp.status_code}: {resp.text[:300]}"
        return parse_response(resp.json()), rtt, None
    except requests.exceptions.Timeout:
        return None, None, f"Timed out after {TIMEOUT_SEC}s"
    except Exception as exc:
        return None, None, str(exc)


def diff_cell(val_a, val_b, label):
    a = str(val_a or "N/A").strip()
    b = str(val_b or "N/A").strip()
    if a == "N/A" or b == "N/A":
        color, badge = "#b45309", "⚠️ Missing"
    elif a.lower() == b.lower():
        color, badge = "#15803d", "✅ Match"
    else:
        color, badge = "#b91c1c", "❌ Differ"

    st.markdown(
        f'<div style="border-left:4px solid {color};padding:6px 12px;'
        f'border-radius:4px;margin-bottom:4px;font-size:0.8rem;'
        f'color:{color};font-weight:600">{badge} — {label}</div>',
        unsafe_allow_html=True,
    )
    c1, c2 = st.columns(2)
    with c1: st.code(a, language=None)
    with c2: st.code(b, language=None)


# ─────────────────────── PAGE ────────────────────────────────────
st.set_page_config(page_title="Kannada ASR — A/B Compare", layout="wide")
st.title("🎙️ Kannada ASR — Side-by-Side Comparison")
st.caption(f"**{LABEL_A}** vs **{LABEL_B}**  |  Host: `{BACKEND_HOST}`")
st.markdown("---")

# ── 1. Audio input ───────────────────────────────────────────────
st.subheader("1 · Provide Kannada audio")

input_method = st.radio(
    "Choose input method:",
    ["🎤  Record with microphone", "📁  Upload WAV file"],
    horizontal=True,
)

audio_bytes = None
if "Record" in input_method:
    af = st.audio_input("Record Kannada audio")
    if af:
        audio_bytes = af.getvalue()
else:
    uf = st.file_uploader("Upload WAV", type=["wav"])
    if uf:
        audio_bytes = uf.read()

if audio_bytes is None:
    st.info("👆 Provide audio above to continue.")
    st.stop()

st.success("✅ Audio ready")
st.audio(audio_bytes, format="audio/wav")
st.markdown("---")

# ── 2. Run ───────────────────────────────────────────────────────
st.subheader("2 · Run comparison")

for k in ("result_a","result_b","rtt_a","rtt_b","err_a","err_b","filename","log_errors"):
    if k not in st.session_state:
        st.session_state[k] = None

if st.button("▶  Run both models", type="primary"):
    filename = make_filename()
    st.session_state["filename"]   = filename
    st.session_state["log_errors"] = []

    # Save locally
    try:
        with open(os.path.join(SAVE_DIR, filename), "wb") as f:
            f.write(audio_bytes)
    except Exception as exc:
        st.warning(f"Local save failed: {exc}")

    # Parallel backend calls
    bucket = {}
    def _call(port):
        bucket[port] = call_backend(port, filename, audio_bytes)

    ta = threading.Thread(target=_call, args=(PORT_A,), daemon=True)
    tb = threading.Thread(target=_call, args=(PORT_B,), daemon=True)
    ta.start(); tb.start()

    with st.spinner("Calling both ports in parallel…"):
        ta.join(timeout=TIMEOUT_SEC)
        tb.join(timeout=TIMEOUT_SEC)
        if PORT_A in bucket and PORT_B not in bucket:
            st.toast(f"⏳ {LABEL_A} done — waiting {GRACE_SEC}s for {LABEL_B}…")
            tb.join(timeout=GRACE_SEC)
        elif PORT_B in bucket and PORT_A not in bucket:
            st.toast(f"⏳ {LABEL_B} done — waiting {GRACE_SEC}s for {LABEL_A}…")
            ta.join(timeout=GRACE_SEC)

    ra, rtt_a, err_a = bucket.get(PORT_A, (None, None, f"{LABEL_A} did not respond"))
    rb, rtt_b, err_b = bucket.get(PORT_B, (None, None, f"{LABEL_B} did not respond"))

    st.session_state.update(
        result_a=ra, rtt_a=rtt_a, err_a=err_a,
        result_b=rb, rtt_b=rtt_b, err_b=err_b,
    )

    # ── GDrive + Sheets logging from Streamlit Cloud ──────────────
    # Fired in background threads so UI is not blocked
    for port, res, rtt in [(PORT_A, ra, rtt_a), (PORT_B, rb, rtt_b)]:
        if res and not (port == PORT_A and err_a) and not (port == PORT_B and err_b):
            threading.Thread(
                target=log_to_sheet,
                kwargs=dict(
                    audio_bytes = audio_bytes,
                    filename    = filename,
                    port        = port,
                    raw_k       = res["raw_kannada"],
                    corrected   = res["corrected_kannada"],
                    english     = res["english_translation"],
                    rtt         = rtt or 0.0,
                ),
                daemon=True,
            ).start()

# ── 3. Results ───────────────────────────────────────────────────
st.markdown("---")
st.subheader("3 · Results")

ra    = st.session_state["result_a"]
rb    = st.session_state["result_b"]
err_a = st.session_state["err_a"]
err_b = st.session_state["err_b"]
rtt_a = st.session_state["rtt_a"]
rtt_b = st.session_state["rtt_b"]

if ra is None and rb is None and err_a is None and err_b is None:
    st.info("Hit **Run both models** to see results.")
    st.stop()

if st.session_state.get("filename"):
    st.caption(f"📁 File: `{st.session_state['filename']}`")

# Log errors (non-blocking)
if st.session_state.get("log_errors"):
    for e in st.session_state["log_errors"]:
        st.warning(f"Sheet/Drive log error: {e}")

# RTT metrics
m1, m2, m3 = st.columns(3)
with m1:
    st.metric(f"RTT — {LABEL_A}", f"{rtt_a} s" if rtt_a else "—")
with m2:
    st.metric(f"RTT — {LABEL_B}", f"{rtt_b} s" if rtt_b else "—",
              delta=f"{round(rtt_b - rtt_a, 3)} s" if rtt_a and rtt_b else None,
              delta_color="inverse")
with m3:
    if rtt_a and rtt_b:
        st.metric("Faster", LABEL_A if rtt_a < rtt_b else LABEL_B,
                  delta=f"{abs(round(rtt_b - rtt_a, 3))} s")

st.markdown("---")

if err_a: st.error(f"**{LABEL_A}:** {err_a}")
if err_b: st.error(f"**{LABEL_B}:** {err_b}")
if err_a and err_b: st.stop()

# Column headers
h1, h2 = st.columns(2)
with h1: st.markdown(f"### 🅰 {LABEL_A}")
with h2: st.markdown(f"### 🅱 {LABEL_B}")
st.markdown("---")

# Field comparisons
for fkey, flabel in [
    ("raw_kannada",         "Raw Kannada transcript"),
    ("corrected_kannada",   "Corrected Kannada"),
    ("english_translation", "English translation"),
    ("audio_duration",      "Audio duration (s)"),
    ("file",                "Saved filename"),
    ("slowed_applied",      "Slowed audio applied"),
    ("speed_factor",        "Speed factor"),
]:
    diff_cell(
        ra.get(fkey, "N/A") if ra else "ERROR",
        rb.get(fkey, "N/A") if rb else "ERROR",
        flabel,
    )
    st.markdown("")

diff_cell(
    f"{rtt_a} s" if rtt_a else "—",
    f"{rtt_b} s" if rtt_b else "—",
    "Round-trip time (RTT)",
)

# Debug
st.markdown("---")
d1, d2 = st.columns(2)
with d1:
    with st.expander(f"DEBUG — {LABEL_A}"):
        st.json(ra["_raw"] if ra else err_a or "No response")
with d2:
    with st.expander(f"DEBUG — {LABEL_B}"):
        st.json(rb["_raw"] if rb else err_b or "No response")
