import io
import os
import json
import time
import asyncio
import hashlib
import requests
import numpy as np
import streamlit as st
from datetime import datetime, timezone, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ─────────────────────── CONFIG ─────────────────────────────────
NGROK_URL      = "https://detective-ethically-thus.ngrok-free.dev"
TIMEOUT_SEC    = 240
CHUNK_STEP     = int(0.16 * 16000)   # 160 ms @ 16 kHz = 2560 samples = 5120 bytes PCM16

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
    return DATA_START_ROW + len(_all_filenames(sheets))


def log_to_sheet(audio_bytes, filename, transcription, translation, rtt):
    try:
        audio_url = upload_to_cloudinary(audio_bytes, filename)
        sheets    = _sheets_client()
        row_idx   = _next_empty_row(sheets)
        cell_a    = f'=HYPERLINK("{audio_url}","{filename}")'
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A{row_idx}:E{row_idx}",
            valueInputOption="USER_ENTERED",
            body={"values": [[
                cell_a,
                transcription,
                transcription,
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
        st.error(f"Audio conversion failed (soundfile: {e1} | PyAV: {e2})")
        st.stop()


def wav_to_pcm16_blocks(wav_bytes: bytes) -> list[bytes]:
    """Strip WAV header, return list of raw PCM16 chunks (160 ms each)."""
    import wave as _wave
    with _wave.open(io.BytesIO(wav_bytes)) as wf:
        raw = wf.readframes(wf.getnframes())
    # CHUNK_STEP samples × 2 bytes/sample
    block_bytes = CHUNK_STEP * 2
    return [raw[i:i + block_bytes] for i in range(0, len(raw), block_bytes)]


# ─────────────────────── BATCH BACKEND ──────────────────────────
def call_batch(filename, audio_bytes):
    try:
        t0   = time.perf_counter()
        resp = requests.post(
            f"{NGROK_URL}/transcribe",
            files={"file": (filename, io.BytesIO(audio_bytes), "audio/wav")},
            timeout=TIMEOUT_SEC,
        )
        rtt = round(time.perf_counter() - t0, 3)
        if resp.status_code != 200:
            return None, rtt, f"HTTP {resp.status_code}: {resp.text[:600]}"
        data = resp.json()
        return {
            "transcription": data.get("text", ""),
            "translation":   "",
            "timing": {
                "asr_seconds":         data.get("duration"),
                "translation_seconds": None,
            },
            "_raw": data,
        }, rtt, None
    except requests.exceptions.Timeout:
        return None, None, f"Timed out after {TIMEOUT_SEC}s"
    except Exception as exc:
        return None, None, str(exc)


# ─────────────────────── STREAMING BACKEND ──────────────────────
def _pcm_to_float32(pcm_bytes: bytes) -> np.ndarray:
    return np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32) / 32768.0


def _to_pcm16(audio_float: np.ndarray) -> bytes:
    return (np.clip(audio_float, -1.0, 1.0) * 32767).astype("<i2").tobytes()


async def _stream_ws(ws_url: str, blocks: list[bytes],
                     partial_placeholder, segments_placeholder,
                     status_placeholder) -> dict:
    """
    Connect to /ws/transcribe, send all blocks as fast as possible,
    update Streamlit placeholders live as events arrive.
    Returns {"transcription": str, "metrics": dict, "_raw_events": list}
    """
    import websockets

    all_events   = []
    segments     = []          # confirmed segment texts
    final_text   = ""
    metrics      = {}

    async with websockets.connect(ws_url, max_size=None) as ws:
        # ── handshake ────────────────────────────────────────────
        ready = json.loads(await ws.recv())
        status_placeholder.caption(
            f"🔗 Connected · {ready.get('sample_rate')} Hz · "
            f"{ready.get('chunk_ms')} ms chunks"
        )
        await ws.send(json.dumps({"type": "config", "format": "int16"}))

        done = asyncio.Event()

        # ── receiver coroutine ───────────────────────────────────
        async def receive():
            nonlocal final_text, metrics
            async for raw in ws:
                event = json.loads(raw)
                all_events.append(event)
                kind = event.get("type")

                if kind == "partial":
                    partial_placeholder.markdown(
                        f"*…{event.get('partial', '')}*"
                    )

                elif kind == "segment":
                    segments.append(event.get("text", ""))
                    partial_placeholder.empty()
                    segments_placeholder.code(
                        "\n".join(segments), language=None
                    )

                elif kind == "final" and not event.get("end_of_stream"):
                    # turn final (silence-triggered mid-session)
                    segments.append(event.get("text", ""))
                    partial_placeholder.empty()
                    segments_placeholder.code(
                        "\n".join(segments), language=None
                    )

                elif kind == "final" and event.get("end_of_stream"):
                    final_text = event.get("transcript", "\n".join(segments))
                    metrics    = event.get("metrics", {})
                    done.set()
                    return

                elif kind == "error":
                    status_placeholder.error(f"Server error: {event.get('detail')}")
                    done.set()
                    return

        receiver = asyncio.create_task(receive())

        # ── sender: push all blocks as fast as possible ──────────
        t0 = time.perf_counter()
        for block_pcm16 in blocks:
            # server expects float32 converted back to PCM16 bytes
            await ws.send(block_pcm16)

        await ws.send(json.dumps({"type": "end"}))
        send_time = round(time.perf_counter() - t0, 3)
        status_placeholder.caption(
            f"📤 Sent {len(blocks)} blocks in {send_time}s · waiting for final…"
        )

        await asyncio.wait_for(done.wait(), timeout=TIMEOUT_SEC)
        receiver.cancel()

    return {
        "transcription": final_text or "\n".join(segments),
        "translation":   "",
        "timing": {
            "asr_seconds":         metrics.get("rtf"),
            "translation_seconds": None,
        },
        "metrics":    metrics,
        "_raw":       {"events": all_events, "metrics": metrics},
    }


def call_stream(audio_bytes, partial_ph, segments_ph, status_ph):
    ws_url = NGROK_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws/transcribe"
    blocks = wav_to_pcm16_blocks(audio_bytes)
    t0     = time.perf_counter()
    try:
        result = asyncio.run(
            _stream_ws(ws_url, blocks, partial_ph, segments_ph, status_ph)
        )
        rtt = round(time.perf_counter() - t0, 3)
        return result, rtt, None
    except asyncio.TimeoutError:
        return None, None, f"Timed out after {TIMEOUT_SEC}s"
    except Exception as exc:
        return None, None, str(exc)


# ════════════════════════════════════════════════════════════════
# PAGE
# ════════════════════════════════════════════════════════════════
st.set_page_config(page_title="Kannada ASR", layout="centered")
st.title("🎙️ Kannada ASR")
st.markdown("---")

# ── Session state ────────────────────────────────────────────────
for k in ("result", "rtt", "err", "filename", "audio_bytes", "mode"):
    if k not in st.session_state:
        st.session_state[k] = None

# ── Mode selector ────────────────────────────────────────────────
mode = st.selectbox(
    "ASR mode",
    ["🚀  Batch (single POST)", "📡  Streaming (WebSocket, live partials)"],
    index=0,
)
is_streaming = "Streaming" in mode

if is_streaming:
    ws_url_display = NGROK_URL.replace("https://", "wss://") + "/ws/transcribe"
    st.caption(f"WebSocket: `{ws_url_display}`")
else:
    st.caption(f"REST: `{NGROK_URL}/transcribe`")

st.markdown("---")

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

import wave as _wv
try:
    with _wv.open(io.BytesIO(audio_bytes)) as wf:
        sr  = wf.getframerate()
        ch  = wf.getnchannels()
        dur = round(wf.getnframes() / sr, 2)
    st.caption(f"WAV ready: {sr} Hz · {ch}ch · {dur}s · {len(audio_bytes)//1024} KB")
except Exception as e:
    st.warning(f"WAV check failed: {e}")

st.success("✅ Audio ready")
st.audio(audio_bytes, format="audio/wav")
st.markdown("---")

# ── 2. Run ───────────────────────────────────────────────────────
st.subheader("2 · Transcribe")

if st.button("▶  Run", type="primary"):
    filename = make_filename()
    st.session_state.update({
        "filename":    filename,
        "audio_bytes": audio_bytes,
        "result":      None,
        "rtt":         None,
        "err":         None,
        "mode":        "stream" if is_streaming else "batch",
    })

    if is_streaming:
        st.markdown("**Live output**")
        status_ph   = st.empty()
        partial_ph  = st.empty()
        segments_ph = st.empty()

        result, rtt, err = call_stream(
            audio_bytes, partial_ph, segments_ph, status_ph
        )
        partial_ph.empty()
        if result:
            status_ph.success(f"✅ Done · RTT {rtt}s")
    else:
        with st.spinner("Calling ASR service…"):
            result, rtt, err = call_batch(filename, audio_bytes)

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
raw      = result.get("_raw", {})
metrics  = result.get("metrics", raw.get("metrics", {}))
c1, c2, c3 = st.columns(3)
c1.metric("RTT",          f"{rtt} s")
c2.metric("Duration",     f"{raw.get('duration', metrics.get('duration', '—'))} s")
c3.metric("RTF",          f"{metrics.get('rtf', raw.get('metrics', {}).get('rtf', '—'))}")

st.markdown("---")
st.markdown("**ಕನ್ನಡ ಲಿಪ್ಯಂತರಣ (Kannada Transcription)**")
st.code(result["transcription"] or "(empty)", language=None)

with st.expander("DEBUG — Raw response"):
    st.json(raw)
