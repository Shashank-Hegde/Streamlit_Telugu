"""
Telugu Audio Browser – Streamlit app
=====================================
Mirrors the Hindi audio browser but points at the Telugu_Audio root folder.

Streamlit secrets required
--------------------------
[gcp_service_account]          – full service-account JSON (same key as Hindi app)
TELUGU_GDRIVE_ROOT_FOLDER_ID   – Drive folder-id of the "Telugu_Audio" root

Folder structure expected
--------------------------
Telugu_Audio/
  Jan/
    Jan01_Telu/  ← WAV files live here (or directly in month folder)
      recording1.wav
      ...
  Feb/
    ...
  Jul/
    Jul30_Telu/
      ...
"""

import io
import re
import base64
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Telugu Audio Browser | O-Health",
    page_icon="🎧",
    layout="wide",
)

AUDIO_EXT_RE = re.compile(r"\.(wav|wave|mp3|ogg|flac|m4a)$", re.IGNORECASE)

# Month order for sorting (handles both "Jan", "January", "01_Jan", etc.)
MONTH_ORDER = {
    m: i for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}

def month_sort_key(name: str) -> int:
    """Sort folder names so months appear Jan→Dec regardless of Drive ordering."""
    lower = name.lower()
    for abbr, idx in MONTH_ORDER.items():
        if abbr in lower:
            return idx
    return 99  # unknown folders go last


# ── Drive helpers ─────────────────────────────────────────────────────────────

@st.cache_resource
def get_drive_service():
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


@st.cache_data(ttl=120, show_spinner=False)
def list_children(folder_id: str):
    """
    Return (subfolders, audio_files) for *folder_id*.
    Results cached 2 min so repeated sidebar clicks don't hit the API.
    """
    service = get_drive_service()
    q = f"'{folder_id}' in parents and trashed = false"
    all_items, page_token = [], None
    while True:
        res = (
            service.files()
            .list(
                q=q,
                fields="nextPageToken, files(id,name,mimeType,size)",
                pageSize=1000,
                orderBy="name",
                pageToken=page_token,
            )
            .execute()
        )
        all_items.extend(res.get("files", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break

    subfolders = sorted(
        [f for f in all_items if f.get("mimeType") == "application/vnd.google-apps.folder"],
        key=lambda f: (month_sort_key(f["name"]), f["name"].lower()),
    )
    audio = [f for f in all_items if f.get("name") and AUDIO_EXT_RE.search(f["name"])]
    return subfolders, audio


@st.cache_data(ttl=7200, show_spinner=False)
def download_file_bytes(file_id: str) -> bytes:
    """Download and cache audio bytes for up to 2 hours."""
    service = get_drive_service()
    req = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, req, chunksize=4 * 1024 * 1024)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue()


def mime_from_name(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower()
    return {
        "wav": "audio/wav",
        "wave": "audio/wav",
        "mp3": "audio/mpeg",
        "ogg": "audio/ogg",
        "flac": "audio/flac",
        "m4a": "audio/mp4",
    }.get(ext, "audio/wav")


def audio_player(audio_bytes: bytes, mime: str = "audio/wav"):
    """Embed audio player that disables right-click download."""
    b64 = base64.b64encode(audio_bytes).decode()
    html = f"""
    <audio controls controlsList="nodownload noplaybackrate"
           oncontextmenu="return false"
           style="width:100%; border-radius:6px;">
      <source src="data:{mime};base64,{b64}" type="{mime}">
      Your browser does not support the audio element.
    </audio>
    """
    st.components.v1.html(html, height=62)


# ── Session state defaults ────────────────────────────────────────────────────

root_id = st.secrets.get("TELUGU_GDRIVE_ROOT_FOLDER_ID", None)
if not root_id:
    st.error(
        "⚠️ `TELUGU_GDRIVE_ROOT_FOLDER_ID` is missing from Streamlit secrets.\n\n"
        "Add it under `.streamlit/secrets.toml`:\n"
        "```toml\nTELUGU_GDRIVE_ROOT_FOLDER_ID = \"<folder-id>\"\n```"
    )
    st.stop()

ss = st.session_state
ss.setdefault("expanded", set())
ss.setdefault("sel_id", None)
ss.setdefault("sel_name", "")
ss.setdefault("sel_path", [])   # breadcrumb list of (name, id)


# ── Sidebar tree ──────────────────────────────────────────────────────────────

def render_tree(folder_id: str, folder_name: str, depth: int = 0, path: list = None):
    """Recursively render a collapsible folder tree in the sidebar."""
    path = path or []
    expanded_set: set = ss["expanded"]
    is_expanded = folder_id in expanded_set
    is_selected = ss.get("sel_id") == folder_id

    indent = "\u00a0" * (depth * 4)
    arrow  = "▾" if is_expanded else "▸"
    icon   = "📂" if is_expanded else "📁"

    c1, c2 = st.sidebar.columns([1, 8])
    with c1:
        if st.button(arrow, key=f"tog_{folder_id}", help="Expand / collapse"):
            if is_expanded:
                expanded_set.discard(folder_id)
            else:
                expanded_set.add(folder_id)
                list_children(folder_id)        # warm cache
            st.rerun()
    with c2:
        label = f"{indent}{icon} {folder_name}"
        btn_type = "primary" if is_selected else "secondary"
        if st.button(label, key=f"sel_{folder_id}", type=btn_type):
            ss["sel_id"]   = folder_id
            ss["sel_name"] = folder_name
            ss["sel_path"] = path + [(folder_name, folder_id)]
            expanded_set.add(folder_id)
            list_children(folder_id)
            st.rerun()

    if is_expanded:
        subfolders, _ = list_children(folder_id)
        for sf in subfolders:
            render_tree(
                sf["id"], sf["name"],
                depth + 1,
                path + [(folder_name, folder_id)],
            )


with st.sidebar:
    st.markdown("## 🎙️ Telugu Audio")
    st.caption("O-Health | Folder Browser")

    col_r, col_c = st.columns([1, 1])
    with col_r:
        if st.button("🔄 Refresh", use_container_width=True):
            st.cache_data.clear()
            ss["expanded"] = set()
            ss["sel_id"]   = None
            ss["sel_name"] = ""
            ss["sel_path"] = []
            st.rerun()
    with col_c:
        if st.button("🏠 Root", use_container_width=True,
                     type="primary" if ss.get("sel_id") == root_id else "secondary"):
            ss["sel_id"]   = root_id
            ss["sel_name"] = "Telugu_Audio"
            ss["sel_path"] = [("Telugu_Audio", root_id)]
            st.rerun()

    st.divider()

    # Render top-level children (month folders)
    try:
        root_subfolders, _ = list_children(root_id)
        if not root_subfolders:
            st.info("No sub-folders found in Telugu_Audio root.")
        for folder in root_subfolders:
            render_tree(folder["id"], folder["name"], depth=0,
                        path=[("Telugu_Audio", root_id)])
    except Exception as e:
        st.error(f"Drive error: {e}")


# ── Main panel ────────────────────────────────────────────────────────────────

# Custom CSS tweaks
st.markdown(
    """
    <style>
      /* Tighter expander header */
      details > summary { font-size: 0.9rem; }
      /* Compact audio player spacing */
      .stExpander { margin-bottom: 4px !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown("# 🎧 Telugu Audio Browser")
st.caption("O-Health · Clinical ASR Dataset · Telugu")

sel_id   = ss.get("sel_id")
sel_name = ss.get("sel_name", "")
sel_path = ss.get("sel_path", [])

if not sel_id:
    st.info("👈 Select a month or daily folder from the sidebar to browse audio files.")
    st.stop()

# ── Breadcrumb ────────────────────────────────────────────────────────────────
if sel_path:
    crumbs = " › ".join(name for name, _ in sel_path)
    st.markdown(f"**📍 {crumbs}**")

st.subheader(f"📂 {sel_name}")

# ── Sub-folder quick-nav ──────────────────────────────────────────────────────
subfolders, audio_files = list_children(sel_id)

if subfolders:
    st.markdown("**Sub-folders**")
    cols = st.columns(min(len(subfolders), 4))
    for i, sf in enumerate(subfolders):
        with cols[i % 4]:
            if st.button(f"📁 {sf['name']}", key=f"nav_{sf['id']}", use_container_width=True):
                ss["sel_id"]   = sf["id"]
                ss["sel_name"] = sf["name"]
                ss["sel_path"] = sel_path + [(sf["name"], sf["id"])]
                ss["expanded"].add(sf["id"])
                list_children(sf["id"])
                st.rerun()
    st.divider()

# ── Filter & pagination controls ─────────────────────────────────────────────
col_search, col_page_size = st.columns([3, 1])
with col_search:
    query = st.text_input("🔍 Search by filename", "", placeholder="e.g. patient, 2024, recording…")
with col_page_size:
    page_size = st.number_input("Files / page", min_value=5, max_value=500, value=30, step=5)

if query.strip():
    audio_files = [f for f in audio_files if query.strip().lower() in f["name"].lower()]

total = len(audio_files)
st.caption(f"**{total}** audio file(s) in this folder")

if not audio_files:
    if subfolders:
        st.info("No audio files directly here — pick a sub-folder above to go deeper.")
    else:
        st.warning("This folder is empty or contains no supported audio files.")
    st.stop()

# ── Pagination ────────────────────────────────────────────────────────────────
page_count = max(1, (total + page_size - 1) // page_size)
page = st.number_input("Page", min_value=1, max_value=page_count, value=1, step=1,
                       label_visibility="collapsed" if page_count == 1 else "visible")

if page_count > 1:
    st.caption(f"Page {page} of {page_count}")

start = (page - 1) * page_size
end   = min(start + page_size, total)
st.divider()

# ── File list with lazy audio loading ────────────────────────────────────────
for idx, f in enumerate(audio_files[start:end], start=start + 1):
    size_kb = int(f.get("size", 0)) // 1024
    size_str = f"({size_kb:,} KB)" if size_kb else ""
    header = f"#{idx}  🎵  {f['name']}  {size_str}"

    with st.expander(header, expanded=False):
        col_play, col_info = st.columns([4, 1])
        with col_info:
            st.caption(f"**ID:** `{f['id'][:12]}…`")
            if size_kb:
                st.caption(f"**Size:** {size_kb:,} KB")

        with col_play:
            with st.spinner("Loading audio…"):
                try:
                    audio_bytes = download_file_bytes(f["id"])
                    mime = mime_from_name(f["name"])
                    audio_player(audio_bytes, mime)
                except Exception as e:
                    st.error(f"Could not load file: {e}")

st.divider()
st.caption("O-Health · Telugu ASR Dataset Browser · Powered by Streamlit + Google Drive API")
