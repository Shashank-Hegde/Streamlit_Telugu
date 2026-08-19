import io
import os
import time
import hashlib
import requests
import streamlit as st
from datetime import datetime, timezone, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ─────────────────────── CONFIG ─────────────────────────────────
NGROK_URL      = "https://detective-ethically-thus.ngrok-free.dev/transcribe"
LANGUAGE_ID    = "kn"
TIMEOUT_SEC    = 240

SHEET_ID       = "1HmP5c0xR3CuvkDakip4J5pdzB6hssy-XRuoOu6iBxNI"
SHEET_TAB      = "Sheet1"
DATA_START_ROW = 3

CLOUDINARY_CLOUD  = "dfufhdc8j"
CLOUDINARY_PRESET = "kannada_asr"

IST = timezone(timedelta(hours=5, minutes=30))

# ─────────────────────── GOOGLE SHEETS CLIENT ───────────────────
@st.cache_resource
def _sheets_client():
    key_dict = dict(st.secrets["gcp_service_account"])
    creds = service_account.Credentials.from_service_account_info(
        key_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


# ─────────────────────── CLOUDINARY UPLOAD ──────────────────────
def upload_to_cloudinary(audio_bytes: bytes, filename: str) -> str:
    api_key    = st.secrets["cloudinary"]["api_key"]
    api_secret = st.secrets["cloudinary"]["api_secret"]
    ts         = str(int(time.time()))
    public_id  = filename.replace(".wav", "")
    sig_str    = "public_id=" + public_id + "&timestamp=" + ts + api_secret
    signature  = hashlib.sha256(sig_str.encode()).hexdigest()
    url        = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD}/raw/upload"
    resp = requests.post(url, data={
        "api_key":   api_key,
        "timestamp": ts,
        "public_id": public_id,
        "signature": signature,
    }, files={"file": (filename, audio_bytes, "audio/wav")}, timeout=60)
    resp.raise_for_status()
    return resp.json()["secure_url"]


# ─────────────────────── SHEETS HELPERS ─────────────────────────
def _all_filenames(sheets) -> list:
    result = (
        sheets.spreadsheets().values()
        .get(spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A{DATA_START_ROW}:A")
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


def _next_empty_row(sheets) -> int:
    names = _all_filenames(sheets)
    return DATA_START_ROW + len(names)


def log_to_sheet(audio_bytes, filename, transcription, translation, rtt):
    """Upload audio to Cloudinary, then write one row to the sheet."""
    try:
        audio_url = upload_to_cloudinary(audio_bytes, filename)
        sheets    = _sheets_client()
        row_idx   = _next_empty_row(sheets)

        cell_a = f'=HYPERLINK("{audio_url}","{filename}")'

        # Columns: A=filename, B=transcription, C=transcription(copy), D=translation, E=RTT
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A{row_idx}:E{row_idx}",
            valueInputOption="USER_ENTERED",
            body={"values": [[
                cell_a,
                transcription,
                transcription,   # col 3 = literal copy of col 2
                translation,
                round(rtt, 3),
            ]]},
        ).execute()
        return True, f"Row {row_idx} written | {audio_url}"
    except Exception as exc:
        import traceback
        return False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"


# ─────────────────────── AUDIO HELPERS ──────────────────────────
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


def to_wav(data: bytes) -> bytes:
    import wave as _wave
    try:
        with _wave.open(io.BytesIO(data)):
            return data
    except Exception:
        pass

    try:
        import soundfile as sf
        import numpy as np
        audio_np, sr = sf.read(io.BytesIO(data), dtype="int16", always_2d=False)
        if sr != 16000:
            target_len = int(len(audio_np) / sr * 16000)
            audio_np = np.interp(
                np.linspace(0, len(audio_np) - 1, target_len),
                np.arange(len(audio_np)),
                audio_np.astype(np.float64)
            ).astype(np.int16)
        if audio_np.ndim == 2:
            audio_np = audio_np.mean(axis=1).astype(np.int16)
        buf = io.BytesIO()
        sf.write(buf, audio_np, 16000, format="WAV", subtype="PCM_16")
        buf.seek(0)
        return buf.read()
    except Exception as e1:
        pass

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


def call_backend(filename, audio_bytes):
    try:
        t0   = time.perf_counter()
        resp = requests.post(
            NGROK_URL,
            files={"audio": (filename, io.BytesIO(audio_bytes), "audio/wav")},
            data={"language_id": LANGUAGE_ID},
            timeout=TIMEOUT_SEC,
        )
        rtt = round(time.perf_counter() - t0, 3)
        if resp.status_code != 200:
            return None, rtt, f"HTTP {resp.status_code}: {resp.text[:600]}"
        data = resp.json()
        return {
            "transcription": data.get("transcription", ""),
            "translation":   data.get("translation", ""),
            "timing":        data.get("timing", {}),
            "_raw":          data,
        }, rtt, None
    except requests.exceptions.Timeout:
        return None, None, f"Timed out after {TIMEOUT_SEC}s"
    except Exception as exc:
        return None, None, str(exc)


# ─────────────────────── PAGE ────────────────────────────────────
st.set_page_config(page_title="Kannada ASR", layout="centered")
st.title("🎙️ Kannada ASR")
st.caption(f"Endpoint: `{NGROK_URL}` · language_id: `{LANGUAGE_ID}`")
st.markdown("---")

# ── Session state init ───────────────────────────────────────────
for k in ("result", "rtt", "err", "filename", "audio_bytes"):
    if k not in st.session_state:
        st.session_state[k] = None

# ── 1. Audio input ───────────────────────────────────────────────
st.subheader("1 · Provide Kannada audio")

input_method = st.radio(
    "Choose input method:",
    ["🎤  Record with microphone", "📁  Upload WAV file"],
    horizontal=True,
)

raw_bytes = None

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

audio_bytes = to_wav(raw_bytes)

# Audio info
import wave as _wv
try:
    with _wv.open(io.BytesIO(audio_bytes)) as wf:
        sr  = wf.getframerate()
        ch  = wf.getnchannels()
        dur = round(wf.getnframes() / sr, 2)
    st.caption(f"WAV ready: {sr}Hz · {ch}ch · {dur}s · {len(audio_bytes)//1024}KB")
except Exception as e:
    st.warning(f"WAV check failed: {e}")

st.success("✅ Audio ready")
st.audio(audio_bytes, format="audio/wav")
st.markdown("---")

# ── 2. Run ───────────────────────────────────────────────────────
st.subheader("2 · Transcribe")

if st.button("▶  Run", type="primary"):
    filename = make_filename()
    st.session_state["filename"]    = filename
    st.session_state["audio_bytes"] = audio_bytes

    with st.spinner("Calling ASR service…"):
        result, rtt, err = call_backend(filename, audio_bytes)

    st.session_state["result"] = result
    st.session_state["rtt"]    = rtt
    st.session_state["err"]    = err

    if result and not err:
        with st.spinner("Logging to Google Sheet…"):
            ok, msg = log_to_sheet(
                audio_bytes   = audio_bytes,
                filename      = filename,
                transcription = result["transcription"],
                translation   = result["translation"],
                rtt           = rtt or 0.0,
            )
        if ok:
            st.success(f"✅ Sheet: {msg}")
        else:
            st.error(f"❌ Sheet log failed: {msg}")

# ── 3. Results ───────────────────────────────────────────────────
st.markdown("---")
st.subheader("3 · Results")

result = st.session_state["result"]
err    = st.session_state["err"]
rtt    = st.session_state["rtt"]

if result is None and err is None:
    st.info("Hit **Run** to see results.")
    st.stop()

if st.session_state.get("filename"):
    st.caption(f"📁 `{st.session_state['filename']}`")

if err:
    st.error(f"Error: {err}")
    st.stop()

# Metrics
c1, c2, c3 = st.columns(3)
c1.metric("RTT", f"{rtt} s")
c2.metric("ASR", f"{result['timing'].get('asr_seconds', '—')} s")
c3.metric("Translation", f"{result['timing'].get('translation_seconds', '—')} s")

st.markdown("---")

st.markdown("**ಕನ್ನಡ ಲಿಪ್ಯಂತರಣ (Kannada Transcription)**")
st.code(result["transcription"] or "(empty)", language=None)

st.markdown("**English Translation**")
st.code(result["translation"] or "(empty)", language=None)

with st.expander("DEBUG — Raw response"):
    st.json(result["_raw"])
