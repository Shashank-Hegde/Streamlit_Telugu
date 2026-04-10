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
)

audio_bytes = None

if input_method == "Record with microphone":
    audio_file = st.audio_input("Record Kannada audio")
    if audio_file is not None:
        audio_bytes = audio_file.getvalue()
else:
    uploaded_file = st.file_uploader("Upload WAV", type=["wav"])
    if uploaded_file is not None:
        audio_bytes = uploaded_file.read()

if audio_bytes is None:
    st.info("👆 Provide audio to begin.")
    st.stop()

st.success("Audio ready")
st.audio(audio_bytes, format="audio/wav")
st.markdown("---")

# ---------------- State ----------------
if "result" not in st.session_state:
    st.session_state["result"] = None
if "rtt" not in st.session_state:
    st.session_state["rtt"] = None

# ---------------- Backend call ----------------
st.subheader("2) Send to backend")

IST = timezone(timedelta(hours=5, minutes=30))

if st.button("Run Kannada ASR", type="primary"):
    now = datetime.now(IST)
    filename = f"streamlit_kannada_{now.strftime('%d%m_%Y_%H%M_%S')}.wav"
    url = f"http://{BACKEND_HOST}:{BACKEND_PORT}/convertSpeechToText"

    try:
        start = time.perf_counter()

        resp = requests.post(
            url,
            files={"file": (filename, io.BytesIO(audio_bytes), "audio/wav")},
            timeout=TIMEOUT_SEC,
        )

        st.session_state["rtt"] = round(time.perf_counter() - start, 3)

        if resp.status_code != 200:
            st.session_state["result"] = {"error": resp.text}
        else:
            st.session_state["result"] = resp.json()

    except Exception as e:
        st.session_state["result"] = {"error": str(e)}

# ---------------- Output ----------------
st.markdown("---")
st.subheader("3) Output")

result = st.session_state.get("result")

if not result:
    st.info("Run ASR to see output")
    st.stop()

# RTT
if st.session_state.get("rtt"):
    st.write("RTT:", st.session_state["rtt"], "sec")

# Error
if "error" in result:
    st.error(result["error"])
    st.stop()

# ---------------- DEBUG (IMPORTANT) ----------------
with st.expander("DEBUG: Full backend response"):
    st.json(result)

# ---------------- SAFE PARSING ----------------

entry = None

if "results" in result and isinstance(result["results"], list) and len(result["results"]) > 0:
    entry = result["results"][0]
else:
    st.error("❌ No valid 'results' found in response")
    st.stop()

# Extract fields safely
raw_kannada = entry.get("raw_transcription", "N/A")

corrected_kannada = (
    entry.get("corrected_kannada")
    or entry.get("corrected_hindi")   # backend reality
    or raw_kannada
)

english_translation = (
    entry.get("english_translation")
    or result.get("transcription")
    or "N/A"
)

backend_file = entry.get("file", "N/A")
audio_duration = entry.get("audio_duration_seconds")

# ---------------- Display ----------------
st.markdown("### Kannada transcript")
st.code(raw_kannada)

st.markdown("### Corrected Kannada")
st.code(corrected_kannada)

st.markdown("### English translation")
st.code(english_translation)

if audio_duration:
    st.markdown("### Audio duration (seconds)")
    st.code(str(audio_duration))

st.markdown("### Backend filename")
st.code(backend_file)
