import io
import os
import json
import threading
from datetime import datetime, timezone, timedelta

import requests
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build

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
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return None, sheets


# ─────────────────────── DRIVE UPLOAD ───────────────────────────
def upload_to_drive(audio_bytes: bytes, filename: str) -> str:
    """Drive upload skipped — service accounts cannot write to personal My Drive.
    Returns empty string; sheet row uses plain filename instead of hyperlink."""
    return ""


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
    # Use hyperlink if Drive URL available, otherwise plain filename
    cell_a = (
        f'=HYPERLINK("{drive_url}","{filename}")' if drive_url
        else filename
    )

    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB}!A{row_idx}:J{row_idx}",
        valueInputOption="USER_ENTERED",
        body={"values": [[cell_a,"","","","","","","","",timestamp]]},
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
    """Upload to Drive + write Sheet row. Returns (ok, message) — never raises."""
    try:
        drive_url = upload_to_drive(audio_bytes, filename)
        _, sheets = _google_clients()
        row_idx   = _find_or_create_row(sheets, filename, drive_url)
        _write_port_columns(sheets, row_idx, port, raw_k, corrected, english, rtt)
        return True, f"port {port} → row {row_idx} | {drive_url}"
    except Exception as exc:
        import traceback
        return False, f"port {port} | {type(exc).__name__}: {exc} | {traceback.format_exc()}"


# ─────────────────────── HELPERS ────────────────────────────────
def make_filename() -> str:
    now = datetime.now(IST)
    ms  = now.microsecond // 1000
    return (
        f"streamlit_"
        f"{now.second:02d}{ms:03d}_"
        f"{now.hour:02d}_"
        f"{now.minute:02d}_"
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
            return None, rtt, f"HTTP {resp.status_code}: {resp.text[:600]}"
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

# ── Secrets health check ──────────────────────────────────────────
with st.expander("🔑 GCP Secrets check (expand to verify)", expanded=False):
    try:
        sa = st.secrets["gcp_service_account"]
        st.success(f"✅ Secret loaded — client_email: {sa['client_email']}")
        st.write(f"project_id: {sa['project_id']}")
        st.write(f"private_key starts with: {sa['private_key'][:40]}...")
    except Exception as e:
        st.error(f"❌ Secret load failed: {e}")

# ── 1. Audio input ───────────────────────────────────────────────
st.subheader("1 · Provide Kannada audio")

input_method = st.radio(
    "Choose input method:",
    ["🎤  Record with microphone", "📁  Upload WAV file"],
    horizontal=True,
)

audio_bytes = None
raw_bytes   = None   # original bytes before any conversion

if "Record" in input_method:
    af = st.audio_input("Record Kannada audio")
    if af:
        raw_bytes = af.getvalue()
else:
    uf = st.file_uploader("Upload WAV", type=["wav"])
    if uf:
        raw_bytes = uf.read()

if raw_bytes is None:
    st.info("👆 Provide audio above to continue.")
    st.stop()

# ── Ensure bytes are valid WAV (microphone returns WebM/OGG) ──
def _to_wav(data: bytes) -> bytes:
    """
    Convert any audio to 16kHz mono WAV.
    Strategy:
      1. Already valid WAV → return as-is
      2. Try soundfile (handles WAV/FLAC/OGG natively, no ffmpeg)
      3. Try av (PyAV — pure Python, no ffmpeg binary needed)
    """
    import wave as _wave
    # Fast path: already a valid WAV
    try:
        with _wave.open(io.BytesIO(data)):
            return data
    except Exception:
        pass

    # Try soundfile (handles ogg/flac/wav without ffmpeg)
    try:
        import soundfile as sf
        import numpy as np
        audio_np, sr = sf.read(io.BytesIO(data), dtype="int16", always_2d=False)
        # Resample to 16kHz if needed using numpy (simple linear interp)
        if sr != 16000:
            duration = len(audio_np) / sr
            target_len = int(duration * 16000)
            audio_np = np.interp(
                np.linspace(0, len(audio_np) - 1, target_len),
                np.arange(len(audio_np)),
                audio_np.astype(np.float64)
            ).astype(np.int16)
        # Convert to mono if stereo
        if audio_np.ndim == 2:
            audio_np = audio_np.mean(axis=1).astype(np.int16)
        # Write as WAV
        buf = io.BytesIO()
        sf.write(buf, audio_np, 16000, format="WAV", subtype="PCM_16")
        buf.seek(0)
        return buf.read()
    except Exception as e1:
        pass

    # Try PyAV (pure Python decoder, no system ffmpeg binary)
    try:
        import av
        import numpy as np
        container = av.open(io.BytesIO(data))
        stream = container.streams.audio[0]
        frames = []
        for frame in container.decode(stream):
            arr = frame.to_ndarray()
            if arr.ndim == 2:
                arr = arr.mean(axis=0)
            frames.append(arr.astype(np.float32))
        container.close()
        audio_np = np.concatenate(frames)
        # Normalise float → int16
        audio_np = (audio_np / max(np.abs(audio_np).max(), 1e-6) * 32767).astype(np.int16)
        buf = io.BytesIO()
        import wave as _w
        with _w.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(audio_np.tobytes())
        buf.seek(0)
        return buf.read()
    except Exception as e2:
        st.error(f"Audio conversion failed (soundfile: {e1} | PyAV: {e2})")
        st.stop()

audio_bytes = _to_wav(raw_bytes)

# ── Audio diagnostics ────────────────────────────────────────────
import wave as _wv
try:
    with _wv.open(io.BytesIO(audio_bytes)) as _wf:
        _sr  = _wf.getframerate()
        _ch  = _wf.getnchannels()
        _sw  = _wf.getsampwidth()
        _nf  = _wf.getnframes()
        _dur = round(_nf / _sr, 2)
    st.caption(
        f"WAV ok: {_sr}Hz {_ch}ch {_sw*8}bit {_dur}s "
        f"| wav_size={len(audio_bytes)}B raw_size={len(raw_bytes)}B"
    )
except Exception as _we:
    st.warning(f"WAV header check failed: {_we} | raw[:4]={raw_bytes[:4]}")

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

    # ── GDrive + Sheets logging — synchronous so errors surface ────
    log_results = []
    for port, res, rtt, err in [
        (PORT_A, ra, rtt_a, err_a),
        (PORT_B, rb, rtt_b, err_b),
    ]:
        if res and not err:
            ok, msg = log_to_sheet(
                audio_bytes = audio_bytes,
                filename    = filename,
                port        = port,
                raw_k       = res["raw_kannada"],
                corrected   = res["corrected_kannada"],
                english     = res["english_translation"],
                rtt         = rtt or 0.0,
            )
            log_results.append((ok, msg))
    st.session_state["log_results"] = log_results

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

# Log status
for ok, msg in st.session_state.get("log_results") or []:
    if ok:
        st.success(f"✅ Sheet/Drive: {msg}")
    else:
        st.error(f"❌ Sheet/Drive failed: {msg}")

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
