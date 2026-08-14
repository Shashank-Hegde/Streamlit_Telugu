import io
import os
import hashlib
import time
from datetime import datetime, timezone, timedelta
import requests
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ─────────────────────── CONFIG ─────────────────────────────────
BACKEND_HOST  = "49.200.100.22"
PORT          = 5013
TIMEOUT_SEC   = 240

SHEET_ID      = "1PsiI62u9IM63AP4nlFutYy84qUuaw7ZSCUFzD9xSe2E"
SHEET_TAB     = "Sheet1"
DATA_START_ROW = 2          # Row 1 = headers

CLOUDINARY_CLOUD  = "dfufhdc8j"
CLOUDINARY_FOLDER = "marathi_asr"   # folder inside Cloudinary

SAVE_DIR = os.path.expanduser("~/Streamlit/Audio/Marathi")
os.makedirs(SAVE_DIR, exist_ok=True)

IST = timezone(timedelta(hours=5, minutes=30))

# Sheet columns: A=filename  B=raw_transcription  C=corrected_hindi
#                D=english_translation  E=RTT
HEADERS = ["filename", "raw_transcription", "corrected_hindi",
           "english_translation", "RTT"]


# ─────────────────────── GOOGLE SHEETS CLIENT ───────────────────
@st.cache_resource
def _sheets_client():
    key_dict = dict(st.secrets["gcp_service_account"])
    creds = service_account.Credentials.from_service_account_info(
        key_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _ensure_headers(sheets):
    """Write column headers to row 1 if the sheet is brand-new."""
    result = (
        sheets.spreadsheets().values()
        .get(spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A1:E1")
        .execute()
    )
    existing = result.get("values", [[]])[0] if result.get("values") else []
    if existing != HEADERS:
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A1:E1",
            valueInputOption="RAW",
            body={"values": [HEADERS]},
        ).execute()


def _all_filenames(sheets) -> list:
    result = (
        sheets.spreadsheets().values()
        .get(spreadsheetId=SHEET_ID,
             range=f"{SHEET_TAB}!A{DATA_START_ROW}:A")
        .execute()
    )
    names = []
    for row in result.get("values", []):
        cell = row[0] if row else ""
        # Strip =HYPERLINK("url","name") → name
        if cell.startswith("=HYPERLINK"):
            try:
                cell = cell.split('"')[3]
            except IndexError:
                pass
        names.append(cell)
    return names


def _append_row(sheets, filename, audio_url,
                raw_transcription, corrected_hindi,
                english_translation, rtt):
    """Find existing row or append a new one, then fill all columns."""
    names   = _all_filenames(sheets)
    if filename in names:
        row_idx = DATA_START_ROW + names.index(filename)
    else:
        row_idx = DATA_START_ROW + len(names)

    cell_a = (
        f'=HYPERLINK("{audio_url}","{filename}")' if audio_url
        else filename
    )

    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB}!A{row_idx}:E{row_idx}",
        valueInputOption="USER_ENTERED",
        body={"values": [[
            cell_a,
            raw_transcription,
            corrected_hindi,
            english_translation,
            round(rtt, 3),
        ]]},
    ).execute()
    return row_idx


# ─────────────────────── CLOUDINARY UPLOAD ──────────────────────
def upload_to_cloudinary(audio_bytes: bytes, filename: str) -> str:
    """
    Upload WAV to Cloudinary under the 'marathi_asr/' folder.
    Uses signed upload (api_key + api_secret from Streamlit secrets).

    Cloudinary folder tip:
      Set public_id = "folder_name/file_stem".
      Cloudinary creates the folder automatically on first upload.
      No manual folder creation needed.
    """
    api_key    = st.secrets["cloudinary"]["api_key"]
    api_secret = st.secrets["cloudinary"]["api_secret"]
    ts         = str(int(time.time()))
    stem       = filename.replace(".wav", "")
    public_id  = f"{CLOUDINARY_FOLDER}/{stem}"   # ← creates the folder

    # Signature = SHA-256( "public_id=...&timestamp=..." + api_secret )
    sig_str   = f"public_id={public_id}&timestamp={ts}{api_secret}"
    signature = hashlib.sha256(sig_str.encode()).hexdigest()

    url  = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD}/raw/upload"
    resp = requests.post(
        url,
        data={
            "api_key":   api_key,
            "timestamp": ts,
            "public_id": public_id,
            "signature": signature,
        },
        files={"file": (filename, audio_bytes, "audio/wav")},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["secure_url"]


# ─────────────────────── BACKEND CALL ───────────────────────────
def call_backend(filename: str, audio_bytes: bytes):
    """POST audio to port 5013. Returns (parsed_result, rtt, error)."""
    url = f"http://{BACKEND_HOST}:{PORT}/convertSpeechToText"
    try:
        t0   = time.perf_counter()
        resp = requests.post(
            url,
            files={"file": (filename, io.BytesIO(audio_bytes), "audio/wav")},
            timeout=TIMEOUT_SEC,
        )
        rtt = round(time.perf_counter() - t0, 3)
        if resp.status_code != 200:
            return None, rtt, f"HTTP {resp.status_code}: {resp.text[:600]}"
        return _parse_response(resp.json()), rtt, None
    except requests.exceptions.Timeout:
        return None, None, f"Timed out after {TIMEOUT_SEC}s"
    except Exception as exc:
        return None, None, str(exc)


def _parse_response(data: dict) -> dict:
    entry = data
    if "results" in data and isinstance(data["results"], list) and data["results"]:
        entry = data["results"][0]
    return {
        "raw_transcription":   entry.get("raw_transcription") or entry.get("raw_hindi") or "N/A",
        "corrected_hindi":     entry.get("corrected_hindi") or "N/A",
        "english_translation": entry.get("english_translation") or entry.get("translation") or "N/A",
        "audio_duration":      entry.get("audio_duration_seconds"),
        "file":                entry.get("file", "N/A"),
        "status":              entry.get("status", "N/A"),
        "_raw":                data,
    }


# ─────────────────────── HELPERS ────────────────────────────────
def make_filename() -> str:
    now = datetime.now(IST)
    ms  = now.microsecond // 1000
    return (
        f"streamlit_marathi_"
        f"{now.second:02d}{ms:03d}_"
        f"{now.hour:02d}_"
        f"{now.minute:02d}_"
        f"{now.day:02d}_"
        f"{now.month:02d}_"
        f"{now.year}.wav"
    )


def _to_wav(data: bytes) -> bytes:
    """Convert any browser audio (WebM/OGG) to 16 kHz mono WAV."""
    import wave as _wave
    # Fast path — already valid WAV
    try:
        with _wave.open(io.BytesIO(data)):
            return data
    except Exception:
        pass

    # soundfile (handles OGG/FLAC natively)
    try:
        import soundfile as sf
        import numpy as np
        audio_np, sr = sf.read(io.BytesIO(data), dtype="int16", always_2d=False)
        if sr != 16000:
            tgt = int(len(audio_np) / sr * 16000)
            audio_np = np.interp(
                np.linspace(0, len(audio_np) - 1, tgt),
                np.arange(len(audio_np)),
                audio_np.astype(np.float64),
            ).astype(np.int16)
        if audio_np.ndim == 2:
            audio_np = audio_np.mean(axis=1).astype(np.int16)
        buf = io.BytesIO()
        sf.write(buf, audio_np, 16000, format="WAV", subtype="PCM_16")
        buf.seek(0)
        return buf.read()
    except Exception as e1:
        pass

    # PyAV fallback
    try:
        import av
        import numpy as np
        container = av.open(io.BytesIO(data))
        stream    = container.streams.audio[0]
        frames    = []
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
            wf.setnchannels(1); wf.setsampwidth(2)
            wf.setframerate(16000); wf.writeframes(audio_np.tobytes())
        buf.seek(0)
        return buf.read()
    except Exception as e2:
        st.error(f"Audio conversion failed — soundfile: {e1} | PyAV: {e2}")
        st.stop()


def _result_card(label: str, value, icon: str = ""):
    """Render a tidy labeled result card."""
    st.markdown(
        f"""
        <div style="background:#1e1e2e;border-left:4px solid #7c3aed;
                    border-radius:6px;padding:10px 16px;margin-bottom:10px">
          <div style="font-size:0.75rem;color:#a78bfa;font-weight:600;
                      text-transform:uppercase;letter-spacing:.05em">
            {icon} {label}
          </div>
          <div style="font-size:1rem;color:#e2e8f0;margin-top:4px;
                      white-space:pre-wrap;word-break:break-word">
            {value}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ─────────────────────── PAGE ────────────────────────────────────
st.set_page_config(page_title="Marathi ASR", layout="centered")

st.markdown(
    """
    <h1 style="text-align:center">
        🎙️ Marathi ASR — Speech Transcription
    </h1>
    <p style="text-align:center;color:#94a3b8">
        Transcribes Marathi audio → raw transcript → corrected Hindi → English
    </p>
    """,
    unsafe_allow_html=True,
)
st.markdown("---")

# ── Secrets health check ─────────────────────────────────────────
with st.expander("🔑 GCP Secrets check", expanded=False):
    try:
        sa = st.secrets["gcp_service_account"]
        st.success(f"✅ Loaded — client_email: {sa['client_email']}")
    except Exception as e:
        st.error(f"❌ Secret load failed: {e}")

# ── 1. Audio input ────────────────────────────────────────────────
st.subheader("1 · Provide Marathi audio")
input_method = st.radio(
    "Choose input method:",
    ["🎤  Record with microphone", "📁  Upload WAV file"],
    horizontal=True,
)

raw_bytes = None
if "Record" in input_method:
    af = st.audio_input("Record Marathi audio")
    if af:
        raw_bytes = af.getvalue()
else:
    uf = st.file_uploader("Upload WAV", type=["wav"])
    if uf:
        raw_bytes = uf.read()

if raw_bytes is None:
    st.info("👆 Provide audio above to continue.")
    st.stop()

audio_bytes = _to_wav(raw_bytes)

# WAV diagnostics
import wave as _wv
try:
    with _wv.open(io.BytesIO(audio_bytes)) as wf:
        _sr  = wf.getframerate()
        _ch  = wf.getnchannels()
        _sw  = wf.getsampwidth()
        _nf  = wf.getnframes()
        _dur = round(_nf / _sr, 2)
    st.caption(
        f"WAV ok — {_sr} Hz · {_ch}ch · {_sw*8}-bit · {_dur}s "
        f"| size = {len(audio_bytes):,} B"
    )
except Exception as we:
    st.warning(f"WAV header check failed: {we}")

st.success("✅ Audio ready")
st.audio(audio_bytes, format="audio/wav")
st.markdown("---")

# ── 2. Transcribe ─────────────────────────────────────────────────
st.subheader("2 · Transcribe")

for k in ("result", "rtt", "error", "filename", "audio_url", "log_msg", "log_ok"):
    if k not in st.session_state:
        st.session_state[k] = None

if st.button("▶  Transcribe", type="primary", use_container_width=True):
    filename = make_filename()
    st.session_state["filename"] = filename
    st.session_state["audio_url"] = None
    st.session_state["log_msg"] = None

    # Save locally
    try:
        with open(os.path.join(SAVE_DIR, filename), "wb") as f:
            f.write(audio_bytes)
    except Exception as exc:
        st.warning(f"Local save skipped: {exc}")

    # Backend call
    with st.spinner(f"Sending to port {PORT}…"):
        result, rtt, error = call_backend(filename, audio_bytes)

    st.session_state.update(result=result, rtt=rtt, error=error)

    if result and not error:
        # Upload to Cloudinary
        try:
            with st.spinner("Uploading audio to Cloudinary…"):
                audio_url = upload_to_cloudinary(audio_bytes, filename)
            st.session_state["audio_url"] = audio_url
        except Exception as exc:
            audio_url = None
            st.warning(f"Cloudinary upload failed: {exc}")

        # Log to Google Sheets
        try:
            sheets = _sheets_client()
            _ensure_headers(sheets)
            row_idx = _append_row(
                sheets,
                filename    = filename,
                audio_url   = st.session_state["audio_url"],
                raw_transcription   = result["raw_transcription"],
                corrected_hindi     = result["corrected_hindi"],
                english_translation = result["english_translation"],
                rtt                 = rtt or 0.0,
            )
            st.session_state["log_ok"]  = True
            st.session_state["log_msg"] = (
                f"Row {row_idx} written | "
                + (st.session_state["audio_url"] or "no audio URL")
            )
        except Exception as exc:
            import traceback
            st.session_state["log_ok"]  = False
            st.session_state["log_msg"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"

# ── 3. Results ─────────────────────────────────────────────────────
st.markdown("---")
st.subheader("3 · Results")

result = st.session_state["result"]
rtt    = st.session_state["rtt"]
error  = st.session_state["error"]

if result is None and error is None:
    st.info("Hit **Transcribe** to see results.")
    st.stop()

if st.session_state.get("filename"):
    st.caption(f"📁 File: `{st.session_state['filename']}`")

# Sheet / Cloudinary status
log_ok  = st.session_state.get("log_ok")
log_msg = st.session_state.get("log_msg")
if log_ok is True:
    st.success(f"✅ Logged to Sheet: {log_msg}")
elif log_ok is False:
    st.error(f"❌ Sheet logging failed: {log_msg}")

audio_url = st.session_state.get("audio_url")
if audio_url:
    st.info(f"☁️ Cloudinary: [{st.session_state['filename']}]({audio_url})")

if error:
    st.error(f"❌ Backend error: {error}")
    st.stop()

# RTT metric
col1, col2 = st.columns([1, 2])
with col1:
    st.metric("Round-trip time", f"{rtt} s" if rtt else "—")
with col2:
    if result.get("audio_duration"):
        st.metric("Audio duration", f"{result['audio_duration']:.2f} s")

st.markdown("---")

# Result cards
if result:
    _result_card("Raw Transcription (Marathi)",      result["raw_transcription"],   "📝")
    _result_card("Corrected Hindi",                   result["corrected_hindi"],     "✏️")
    _result_card("English Translation",               result["english_translation"], "🌐")
    _result_card("File (returned by backend)",        result["file"],                "📄")
    _result_card("Status",                            result["status"],              "ℹ️")

# Debug
st.markdown("---")
with st.expander("🛠 Debug — raw backend response"):
    st.json(result["_raw"] if result else error or "No response")
