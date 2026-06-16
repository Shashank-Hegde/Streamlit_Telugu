import io
import os
import time
from datetime import datetime, timezone, timedelta

import requests
import streamlit as st

# ─────────────────────────── CONFIG ────────────────────────────
BACKEND_HOST  = "49.200.100.22"
PORT_A        = 6007          # "Old" model
PORT_B        = 6011          # "New" model
LABEL_A       = "Port 6007"
LABEL_B       = "Port 6011"
TIMEOUT_SEC   = 240
SAVE_DIR      = os.path.expanduser("~/Streamlit/Audio/Kannada")
os.makedirs(SAVE_DIR, exist_ok=True)

IST = timezone(timedelta(hours=5, minutes=30))

# ─────────────────────────── HELPERS ───────────────────────────

def make_filename() -> str:
    """streamlit_<second><ms>_<hour>_<dd>_<mm>_<yyyy>.wav"""
    now = datetime.now(IST)
    ms  = now.microsecond // 1000
    return (
        f"streamlit_"
        f"{now.second:02d}{ms:03d}_"
        f"{now.hour:02d}_"
        f"{now.day:02d}_"
        f"{now.month:02d}_"
        f"{now.year}.wav"
    )


def save_audio(audio_bytes: bytes, filename: str) -> str:
    path = os.path.join(SAVE_DIR, filename)
    with open(path, "wb") as f:
        f.write(audio_bytes)
    return path


def parse_response(data: dict) -> dict:
    """
    Normalise both flat and results[] shapes into a single dict
    with consistent keys: raw_kannada, corrected_kannada,
    english_translation, audio_duration, file, slowed_applied, speed_factor.
    """
    entry = data
    if "results" in data and isinstance(data["results"], list) and data["results"]:
        entry = data["results"][0]

    return {
        "raw_kannada": (
            entry.get("raw_hindi")
            or entry.get("raw_transcription")
            or entry.get("raw_kannada")
            or "N/A"
        ),
        "corrected_kannada": (
            entry.get("corrected_hindi")
            or entry.get("corrected_kannada")
            or "N/A"
        ),
        "english_translation": (
            entry.get("english_translation")
            or entry.get("translation")
            or "N/A"
        ),
        "audio_duration": entry.get("audio_duration_seconds"),
        "file":           entry.get("file", "N/A"),
        "slowed_applied": entry.get("slowed_applied"),
        "speed_factor":   entry.get("speed_factor"),
        "_raw":           data,          # keep full payload for debug
    }


def call_backend(port: int, filename: str, audio_bytes: bytes) -> tuple[dict | None, float | None, str | None]:
    """Returns (parsed_result, rtt_seconds, error_message)."""
    url = f"http://{BACKEND_HOST}:{port}/convertSpeechToText"
    try:
        t0   = time.perf_counter()
        resp = requests.post(
            url,
            files={"file": (filename, io.BytesIO(audio_bytes), "audio/wav")},
            timeout=TIMEOUT_SEC,
        )
        rtt = round(time.perf_counter() - t0, 3)
        if resp.status_code != 200:
            return None, rtt, f"HTTP {resp.status_code}: {resp.text[:300]}"
        return parse_response(resp.json()), rtt, None
    except requests.exceptions.Timeout:
        return None, None, f"Request timed out after {TIMEOUT_SEC}s"
    except Exception as exc:
        return None, None, str(exc)


def diff_cell(val_a: str, val_b: str, key: str):
    """
    Render two values side-by-side.
    Green  = both identical
    Red    = differ
    Orange = one is N/A / missing
    """
    a = str(val_a or "N/A").strip()
    b = str(val_b or "N/A").strip()

    if a == "N/A" or b == "N/A":
        color = "#b45309"   # amber — one side missing
    elif a.lower() == b.lower():
        color = "#15803d"   # green — match
    else:
        color = "#b91c1c"   # red — mismatch

    badge = "✅ Match" if (a.lower() == b.lower() and a != "N/A") else ("⚠️ Missing" if (a == "N/A" or b == "N/A") else "❌ Differ")

    st.markdown(
        f"""
        <div style="border-left:4px solid {color};padding:6px 12px;
                    border-radius:4px;margin-bottom:4px;font-size:0.8rem;
                    color:{color};font-weight:600">{badge} — {key}</div>
        """,
        unsafe_allow_html=True,
    )

    col1, col2 = st.columns(2)
    with col1:
        st.code(a, language=None)
    with col2:
        st.code(b, language=None)


# ─────────────────────────── PAGE ──────────────────────────────
st.set_page_config(page_title="Kannada ASR — A/B Compare", layout="wide")
st.title("🎙️ Kannada ASR — Side-by-Side Comparison")
st.caption(f"**{LABEL_A}** (old pipeline)  vs  **{LABEL_B}** (new pipeline)  |  Host: `{BACKEND_HOST}`")
st.markdown("---")

# ─────────────────────── 1. AUDIO INPUT ────────────────────────
st.subheader("1 · Provide Kannada audio")

input_method = st.radio(
    "Choose input method:",
    ["🎤  Record with microphone", "📁  Upload WAV file"],
    index=0,
    horizontal=True,
)

audio_bytes: bytes | None = None

if "Record" in input_method:
    audio_file = st.audio_input("Record Kannada audio")
    if audio_file is not None:
        audio_bytes = audio_file.getvalue()
else:
    uploaded = st.file_uploader("Upload WAV", type=["wav"])
    if uploaded is not None:
        audio_bytes = uploaded.read()

if audio_bytes is None:
    st.info("👆 Provide audio above to continue.")
    st.stop()

st.success("✅ Audio ready")
st.audio(audio_bytes, format="audio/wav")
st.markdown("---")

# ─────────────────────── 2. RUN ────────────────────────────────
st.subheader("2 · Run comparison")

# Session state
for k in ("result_a", "result_b", "rtt_a", "rtt_b", "err_a", "err_b", "saved_file"):
    if k not in st.session_state:
        st.session_state[k] = None

run_col, info_col = st.columns([1, 3])
with run_col:
    run = st.button("▶  Run both models", type="primary", use_container_width=True)

if run:
    filename = make_filename()

    # Save to disk
    try:
        saved_path = save_audio(audio_bytes, filename)
        st.session_state["saved_file"] = saved_path
    except Exception as exc:
        st.warning(f"Could not save audio to disk: {exc}")
        st.session_state["saved_file"] = None

    # Call both ports — sequential (avoids hammering GPU with parallel hits)
    with st.spinner(f"Calling {LABEL_A} (port {PORT_A})…"):
        ra, rtt_a, err_a = call_backend(PORT_A, filename, audio_bytes)

    with st.spinner(f"Calling {LABEL_B} (port {PORT_B})…"):
        rb, rtt_b, err_b = call_backend(PORT_B, filename, audio_bytes)

    st.session_state.update(
        result_a=ra, rtt_a=rtt_a, err_a=err_a,
        result_b=rb, rtt_b=rtt_b, err_b=err_b,
    )

# ─────────────────────── 3. OUTPUT ─────────────────────────────
st.markdown("---")
st.subheader("3 · Results")

ra   = st.session_state["result_a"]
rb   = st.session_state["result_b"]
err_a = st.session_state["err_a"]
err_b = st.session_state["err_b"]

if ra is None and rb is None and err_a is None and err_b is None:
    st.info("Hit **Run both models** to see results.")
    st.stop()

# ── Saved file path ──
if st.session_state["saved_file"]:
    st.caption(f"💾 Audio saved → `{st.session_state['saved_file']}`")

# ── RTT banner ──
rtt_a = st.session_state["rtt_a"]
rtt_b = st.session_state["rtt_b"]

m1, m2, m3 = st.columns(3)
with m1:
    if rtt_a is not None:
        st.metric(f"RTT — {LABEL_A}", f"{rtt_a} s")
    elif err_a:
        st.metric(f"RTT — {LABEL_A}", "—")
with m2:
    if rtt_b is not None:
        st.metric(f"RTT — {LABEL_B}", f"{rtt_b} s",
                  delta=f"{round(rtt_b - rtt_a, 3)} s vs {LABEL_A}" if rtt_a else None,
                  delta_color="inverse")
    elif err_b:
        st.metric(f"RTT — {LABEL_B}", "—")
with m3:
    if rtt_a and rtt_b:
        faster = LABEL_A if rtt_a < rtt_b else LABEL_B
        st.metric("Faster model", faster, delta=f"{abs(round(rtt_b - rtt_a, 3))} s")

st.markdown("---")

# ── Error display ──
if err_a:
    st.error(f"**{LABEL_A} error:** {err_a}")
if err_b:
    st.error(f"**{LABEL_B} error:** {err_b}")

if err_a and err_b:
    st.stop()

# ── Side-by-side column header ──
hdr1, hdr2 = st.columns(2)
with hdr1:
    st.markdown(f"### 🅰 {LABEL_A}")
with hdr2:
    st.markdown(f"### 🅱 {LABEL_B}")

st.markdown("---")

# ── Field-by-field comparison ──
fields = [
    ("raw_kannada",         "Raw Kannada transcript"),
    ("corrected_kannada",   "Corrected Kannada"),
    ("english_translation", "English translation"),
    ("audio_duration",      "Audio duration (s)"),
    ("file",                "Saved filename"),
    ("slowed_applied",      "Slowed audio applied"),
    ("speed_factor",        "Speed factor"),
]

for fkey, flabel in fields:
    val_a = ra.get(fkey, "N/A") if ra else "ERROR"
    val_b = rb.get(fkey, "N/A") if rb else "ERROR"
    diff_cell(val_a, val_b, flabel)
    st.markdown("")   # breathing room

# ── RTT row ──
diff_cell(
    f"{rtt_a} s" if rtt_a else "—",
    f"{rtt_b} s" if rtt_b else "—",
    "Round-trip time (RTT)",
)

# ── Debug expanders ──
st.markdown("---")
col_dbg1, col_dbg2 = st.columns(2)
with col_dbg1:
    with st.expander(f"DEBUG — Full {LABEL_A} response"):
        if ra:
            st.json(ra["_raw"])
        else:
            st.write(err_a or "No response")
with col_dbg2:
    with st.expander(f"DEBUG — Full {LABEL_B} response"):
        if rb:
            st.json(rb["_raw"])
        else:
            st.write(err_b or "No response")
