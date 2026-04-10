import io
import time
from datetime import datetime, timezone, timedelta

import requests
import streamlit as st

# ---------------- CONFIG ----------------
BACKEND_HOST = "49.200.100.22"
BACKEND_PORT = 6011
TIMEOUT_SEC = 240

st.set_page_config(page_title="Kannada ASR + Translation", layout="wide")
st.title("Kannada ASR + Translation")
st.caption("Upload/record Kannada speech → Kannada transcript + English translation")
st.markdown("---")

# ---------------- Audio input ----------------
st.subheader("1) Provide Kannada audio")
input_method = st.radio(
    "Choose input method:",
    ["Record with microphone", "Upload WAV file"],
    index=0,
    key="audio_input_method",
)

audio_bytes = None

if input_method == "Record with microphone":
    audio_file = st.audio_input(
        "Click to record your Kannada audio, then click again to stop:",
        key="audio_rec",
    )
    if audio_file is not None:
        audio_bytes = audio_file.getvalue()
else:
    uploaded_file = st.file_uploader(
        "Upload a .wav file with Kannada audio:",
        type=["wav"],
        key="audio_upload",
    )
    if uploaded_file is not None:
        audio_bytes = uploaded_file.read()

if audio_bytes is None:
    st.info("👆 Provide audio to begin.")
    st.stop()

st.success("Audio ready.")
st.audio(audio_bytes, format="audio/wav")
st.markdown("---")

# ---------------- State ----------------
if "result" not in st.session_state:
    st.session_state["result"] = None
if "saved_filename" not in st.session_state:
    st.session_state["saved_filename"] = None
if "rtt_seconds" not in st.session_state:
    st.session_state["rtt_seconds"] = None

# ---------------- Backend call ----------------
st.subheader("2) Send to backend")

IST = timezone(timedelta(hours=5, minutes=30))

col_btn, col_info = st.columns([1, 3])

with col_btn:
    if st.button("Run Kannada ASR", type="primary"):
        now = datetime.now(IST)
        timestamp_str = now.strftime("%d%m_%Y_%H%M_%S") + "_" + str(now.microsecond // 1000).zfill(3)
        filename = f"streamlit_kannada_{timestamp_str}.wav"
        url = f"http://{BACKEND_HOST}:{BACKEND_PORT}/convertSpeechToText"

        try:
            start_t = time.perf_counter()
            resp = requests.post(
                url,
                files={"file": (filename, io.BytesIO(audio_bytes), "audio/wav")},
                timeout=TIMEOUT_SEC,
            )
            rtt = time.perf_counter() - start_t
            st.session_state["rtt_seconds"] = round(rtt, 3)

            if resp.status_code != 200:
                st.session_state["result"] = {"error": f"HTTP {resp.status_code}: {resp.text}"}
                st.session_state["saved_filename"] = None
            else:
                data = resp.json()
                st.session_state["result"] = data

                # Prefer nested results[0].file, fallback to top-level file
                saved_filename = None
                if isinstance(data.get("results"), list) and len(data["results"]) > 0:
                    saved_filename = data["results"][0].get("file")
                if not saved_filename:
                    saved_filename = data.get("file")

                st.session_state["saved_filename"] = saved_filename

        except Exception as e:
            st.session_state["result"] = {"error": str(e)}
            st.session_state["rtt_seconds"] = None
            st.session_state["saved_filename"] = None

with col_info:
    if st.session_state.get("saved_filename"):
        st.markdown(f"**Saved on backend server as:** `{st.session_state['saved_filename']}`")
    st.markdown(f"**Backend URL:** `http://{BACKEND_HOST}:{BACKEND_PORT}/convertSpeechToText`")

# ---------------- Show output ----------------
st.markdown("---")
st.subheader("3) Output")

result = st.session_state.get("result")
if not result:
    st.info("Click **Run Kannada ASR** to get transcript + translation.")
    st.stop()

rtt_val = st.session_state.get("rtt_seconds")
if rtt_val is not None:
    st.markdown(f"**RTT (request–response, seconds):** `{rtt_val}`")

if "error" in result:
    st.error(result["error"])
    st.stop()

# ---------------- Parse backend response ----------------
entry = {}
if isinstance(result.get("results"), list) and len(result["results"]) > 0:
    entry = result["results"][0]

raw_kannada = entry.get("raw_transcription") or "N/A"

# IMPORTANT FIX
corrected_kannada = (
    entry.get("corrected_kannada")
    or entry.get("corrected_hindi")   # 👈 fallback (your backend uses this)
    or raw_kannada
)

english_translation = entry.get("english_translation") or result.get("transcription") or "N/A"

backend_file = entry.get("file") or "N/A"
audio_duration = entry.get("audio_duration_seconds")

# ---------------- Display ----------------
st.markdown("**Kannada transcript:**")
st.code(raw_kannada, language="text")

st.markdown("**Corrected Kannada:**")
st.code(corrected_kannada, language="text")

st.markdown("**English translation:**")
st.code(english_translation, language="text")

if audio_duration is not None:
    st.markdown("**Audio duration (seconds):**")
    st.code(str(audio_duration), language="text")

st.markdown("**Backend audio_file field (basename):**")
st.code(backend_file, language="text")

# ---------------- Optional: full JSON ----------------
with st.expander("Show full backend JSON"):
    st.json(result)
