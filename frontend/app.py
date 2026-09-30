"""Operator console shell (S01): shows control-plane readiness.

Run: ``streamlit run frontend/app.py``
"""

from __future__ import annotations

import streamlit as st

from frontend.api_client import AgentApiClient, FrontendSettings

STATUS_LABELS = {"ok": "OK", "degraded": "DEGRADED", "down": "DOWN"}


def main() -> None:
    st.set_page_config(page_title="Self-Healing Pipeline Monitor", layout="wide")
    st.title("Self-Healing Pipeline Monitor")

    client = AgentApiClient.from_settings(FrontendSettings())
    try:
        readiness = client.get_readiness()
    finally:
        client.close()

    st.subheader("Control plane")
    if readiness.error:
        st.error(readiness.error)
        return

    label = STATUS_LABELS.get(readiness.status or "", str(readiness.status).upper())
    # Status is always shown as text, never by colour alone (NFR-050).
    if readiness.status == "ok":
        st.success(f"Backend status: {label}")
    elif readiness.status == "degraded":
        st.warning(f"Backend status: {label}")
    else:
        st.error(f"Backend status: {label}")

    if readiness.components:
        st.table(
            [
                {
                    "Component": name,
                    "Status": STATUS_LABELS.get(str(info.get("status")), str(info.get("status"))),
                    "Detail": info.get("detail") or "",
                }
                for name, info in readiness.components.items()
            ]
        )
    else:
        st.caption("No components registered yet.")


main()
