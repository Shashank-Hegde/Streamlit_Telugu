import socket
import time
import requests
import streamlit as st

st.set_page_config(page_title="Egress Debug", layout="wide")
st.title("Streamlit Cloud Egress Debug")

TARGETS = [
    ("49.200.100.22", 6005),
    ("49.200.100.22", 6006),
    ("49.200.100.22", 443),
    ("example.com", 443),
]

def tcp_connect(host, port, timeout=5):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.perf_counter()
    try:
        s.connect((host, port))
        s.close()
        return True, round(time.perf_counter() - t0, 3), None
    except Exception as e:
        return False, round(time.perf_counter() - t0, 3), str(e)

st.header("1) Raw TCP connect tests (from Streamlit Cloud)")
rows = []
for host, port in TARGETS:
    ok, dt, err = tcp_connect(host, port, timeout=5)
    rows.append({"host": host, "port": port, "ok": ok, "sec": dt, "err": err})
st.json(rows)

st.header("2) HTTP tests (from Streamlit Cloud)")
def http_get(url):
    t0 = time.perf_counter()
    try:
        r = requests.get(url, timeout=8)
        return {"url": url, "status": r.status_code, "sec": round(time.perf_counter() - t0, 3), "body_head": r.text[:200]}
    except Exception as e:
        return {"url": url, "error": repr(e)}

tests = [
    "http://49.200.100.22:6006/docs",
    "http://49.200.100.22:6005/docs",
    "https://example.com",
]
st.json([http_get(u) for u in tests])
