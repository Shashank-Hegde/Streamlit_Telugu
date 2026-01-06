import io
import time

import requests
import streamlit as st

# ---------------- CONFIG ----------------
# IMPORTANT:
# - If Streamlit is running on the SAME server as FastAPI, use 127.0.0.1
# - If Streamlit is running on a DIFFERENT machine / Streamlit Cloud, use the PUBLIC IP
BACKEND_HOST = "49.200.100.22"   # change to "127.0.0.1" only if streamlit runs on same server
BACKEND_PORT = 6006
TIMEOUT_SEC = 240  # longer timeout for big models

st.set_page_config(page_title="Telugu ASR + Translation", layout="wide")
st.title("Telugu ASR + Translation")
st.caption("Upload/record Telugu speech → Telugu transcript + English translation")

st.markdown("---")

# ---------------- Audio input ----------------
st.subheader("1) Provide Telugu audio")

input_method = st.radio(
    "Choose input method:",
    ["Record with microphone", "Upload WAV file"],
    index=0,
    key="audio_input_method",
)

audio_bytes = None

if input_method == "Record with microphone":
    audio_file = st.audio_input(
        "Click to record your Telugu audio, then click again to stop:",
        key="audio_rec",
    )
    if audio_file is not None:
        audio_bytes = audio_file.getvalue()
else:
    uploaded_file = st.file_uploader(
        "Upload a .wav file with Telugu audio:",
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
st.subheader("2) Send to backend (backend saves file + runs ASR)")

# ---------------- State ----------------
if "result" not in st.session_state:
    st.session_state["result"] = None
if "saved_filename" not in st.session_state:
    st.session_state["saved_filename"] = None
if "rtt_seconds" not in st.session_state:
    st.session_state["rtt_seconds"] = None

col_btn, col_info = st.columns([1, 3])

with col_btn:
    if st.button("Run Telugu ASR", type="primary"):
        url = f"http://{BACKEND_HOST}:{BACKEND_PORT}/convertSpeechToText"

        try:
            start_t = time.perf_counter()
            resp = requests.post(
                url,
                files={
                    # Backend expects multipart field name "file"
                    "file": ("streamlit_telugu.wav", io.BytesIO(audio_bytes), "audio/wav")
                },
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
                # Backend returns the saved filename in "uploaded_filename"
                st.session_state["saved_filename"] = data.get("uploaded_filename")

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
    st.info("Click **Run Telugu ASR** to get transcript + translation.")
    st.stop()

rtt_val = st.session_state.get("rtt_seconds")
if rtt_val is not None:
    st.markdown(f"**RTT (request–response, seconds):** `{rtt_val}`")

if "error" in result:
    st.error(result["error"])
    st.stop()

st.markdown("**Telugu transcript:**")
st.code(result.get("telugu_transcript", "N/A"), language="text")

st.markdown("**English translation:**")
st.code(result.get("english_translation", "N/A"), language="text")

st.markdown("**Backend audio_file field (basename):**")
st.code(result.get("audio_file", "N/A"), language="text")
