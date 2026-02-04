#!/usr/bin/env python3
"""Streamlit dashboard for IP camera monitoring."""

from __future__ import annotations

import asyncio
from typing import Dict, List

import pandas as pd
import streamlit as st

from camera_monitor import AlertConfig, CameraResult, SnmpResetConfig, load_cameras, run_checks


def _status_style(value: str) -> str:
    if value == "Aktif":
        return "color: #0f7d0f; font-weight: 700;"
    return "color: #b30000; font-weight: 700;"


def _row_style(row: pd.Series) -> List[str]:
    status = row.get("Durum", "")
    health = row.get("Saglik", "")
    if status != "Aktif":
        return ["background-color: #fde8e8;"] * len(row)
    if health == "Sorunlu":
        return ["background-color: #fff4cc;"] * len(row)
    return [""] * len(row)


def build_table(results: List[CameraResult]) -> pd.DataFrame:
    rows = []
    for result in results:
        rows.append(
            {
                "IP": result.camera.ip,
                "Lokasyon": result.camera.location,
                "Durum": result.status,
                "Ping (ms)": "" if result.ping_ms is None else f"{result.ping_ms:.1f}",
                "Saglik": result.health,
                "Uyari": "Evet" if result.alert_sent else "Hayir",
                "Thumbnail": result.thumbnail_path or "",
            }
        )
    return pd.DataFrame(rows)


def render_table(df: pd.DataFrame) -> None:
    styled = (
        df.style.apply(_row_style, axis=1)
        .applymap(_status_style, subset=["Durum"])
        .format(na_rep="")
    )
    st.dataframe(styled, use_container_width=True)


def select_camera(results: List[CameraResult]) -> CameraResult | None:
    if not results:
        return None
    options: Dict[str, CameraResult] = {
        f"{result.camera.location} ({result.camera.ip})": result for result in results
    }
    selected_label = st.selectbox("Kamera Sec", list(options.keys()))
    return options.get(selected_label)


def render_detail_panel(result: CameraResult | None) -> None:
    st.subheader("Kamera Detaylari")
    if result is None:
        st.info("Detaylari gormek icin once tarama yapin.")
        return

    col1, col2, col3 = st.columns(3)
    col1.metric("IP", result.camera.ip)
    col2.metric("Lokasyon", result.camera.location or "-")
    col3.metric("Durum", result.status)

    col4, col5, col6 = st.columns(3)
    ping_value = "-" if result.ping_ms is None else f"{result.ping_ms:.1f} ms"
    col4.metric("Ping", ping_value)
    col5.metric("Saglik", result.health)
    col6.metric("Uyari", "Evet" if result.alert_sent else "Hayir")

    st.text_area("Detay", result.detail, height=80)
    st.text_area("Saglik Detay", result.health_detail, height=80)

    if result.thumbnail_path:
        st.image(result.thumbnail_path, caption="Son Thumbnail")
    else:
        st.info("Bu kamera icin thumbnail bulunamadi.")


def build_default_configs() -> tuple[AlertConfig, SnmpResetConfig]:
    alert_config = AlertConfig(
        telegram_token="",
        telegram_chat_id="",
        email_to="",
        email_from="",
        smtp_host="",
        smtp_port=587,
        smtp_user="",
        smtp_password="",
        alert_on_health=False,
    )
    snmp_config = SnmpResetConfig(
        enabled=False,
        host="",
        port=161,
        community="",
        ifindex=0,
    )
    return alert_config, snmp_config


def main() -> None:
    st.set_page_config(page_title="IP Kamera Takip Paneli", layout="wide")
    st.title("IP Kamera Takip Paneli")

    with st.sidebar:
        st.header("Ayarlar")
        csv_path = st.text_input("Kamera CSV Yolu", value="sample_cameras.csv")
        concurrency = st.slider("Eszamanli Tarama", min_value=1, max_value=200, value=50)
        stream_timeout = st.slider("RTSP Zaman Asimi (sn)", min_value=1.0, max_value=10.0, value=4.0)
        scan_now = st.button("Tarama Baslat", type="primary")

    if "results" not in st.session_state:
        st.session_state["results"] = []

    if scan_now:
        with st.spinner("Kameralar taraniyor..."):
            try:
                cameras = load_cameras(csv_path)
                alert_config, snmp_config = build_default_configs()
                st.session_state["results"] = asyncio.run(
                    run_checks(
                        cameras,
                        stream_timeout_s=stream_timeout,
                        concurrency=concurrency,
                        alert_config=alert_config,
                        snmp_config=snmp_config,
                    )
                )
            except Exception as exc:
                st.error(f"Tarama hatasi: {exc}")

    results: List[CameraResult] = st.session_state["results"]
    total = len(results)
    aktif = sum(1 for result in results if result.status == "Aktif")
    offline = sum(1 for result in results if result.status != "Aktif")
    health_issues = sum(1 for result in results if result.health == "Sorunlu")

    kpi1, kpi2, kpi3, kpi4 = st.columns(4)
    kpi1.metric("Toplam Kamera", total)
    kpi2.metric("Aktif", aktif)
    kpi3.metric("Offline", offline)
    kpi4.metric("Saglik Sorunu", health_issues)

    if results:
        df = build_table(results)
        render_table(df)
        selected = select_camera(results)
        render_detail_panel(selected)
    else:
        st.info("Tarama yapmak icin sol menuden 'Tarama Baslat' butonuna basin.")


if __name__ == "__main__":
    main()
