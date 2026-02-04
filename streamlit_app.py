#!/usr/bin/env python3
"""Streamlit dashboard for IP camera monitoring."""

from __future__ import annotations

import asyncio
from typing import List

import pandas as pd
import streamlit as st

from camera_monitor import Camera, CameraResult, load_cameras, ping_host


async def check_camera_status(camera: Camera) -> CameraResult:
    ping_ok, ping_ms, ping_detail = await asyncio.to_thread(ping_host, camera.ip)
    status = "ONLINE" if ping_ok else "OFFLINE"
    detail = "Ping OK" if ping_ok else ping_detail
    return CameraResult(
        camera=camera,
        status=status,
        ping_ms=ping_ms,
        detail=detail,
        health="Bilinmiyor",
        health_detail="Saglik kontrolu yapilmadi",
        alert_sent=False,
        alert_detail="Uyari gonderilmedi",
    )


async def run_scan(cameras: List[Camera], concurrency: int) -> List[CameraResult]:
    semaphore = asyncio.Semaphore(concurrency)

    async def _wrapped(camera: Camera) -> CameraResult:
        async with semaphore:
            return await check_camera_status(camera)

    tasks = [asyncio.create_task(_wrapped(camera)) for camera in cameras]
    return await asyncio.gather(*tasks)


def build_table(results: List[CameraResult]) -> pd.DataFrame:
    rows = []
    for result in results:
        rows.append(
            {
                "IP": result.camera.ip,
                "İsim": result.camera.location,
                "Durum": result.status,
            }
        )
    return pd.DataFrame(rows)


def render_status_table(df: pd.DataFrame) -> None:
    def _color_status(value: str) -> str:
        if value == "ONLINE":
            return "color: #0f7d0f; font-weight: 700;"
        if value == "OFFLINE":
            return "color: #b30000; font-weight: 700;"
        return ""

    styled = df.style.applymap(_color_status, subset=["Durum"])
    st.dataframe(styled, use_container_width=True)


def main() -> None:
    st.set_page_config(page_title="IP Kamera Takip Paneli", layout="wide")
    st.title("IP Kamera Takip Paneli")

    with st.sidebar:
        st.header("Genel Durum")
        csv_path = st.text_input("Kamera CSV Yolu", value="sample_cameras.csv")
        concurrency = st.number_input("Eszamanli tarama", min_value=1, max_value=200, value=50, step=1)

    try:
        cameras = load_cameras(csv_path)
    except Exception as exc:
        st.error(f"CSV okunamadi: {exc}")
        return

    if "results" not in st.session_state:
        st.session_state["results"] = []

    if st.button("Taramayı Başlat", type="primary"):
        with st.spinner("Kameralar taraniyor..."):
            st.session_state["results"] = asyncio.run(run_scan(cameras, int(concurrency)))

    results: List[CameraResult] = st.session_state["results"]
    total_count = len(cameras)
    online_count = sum(1 for result in results if result.status == "ONLINE")
    offline_count = sum(1 for result in results if result.status == "OFFLINE")

    with st.sidebar:
        st.metric("Toplam Kamera", total_count)
        st.metric("ONLINE", online_count)
        st.metric("OFFLINE", offline_count)

    if results:
        df = build_table(results)
        render_status_table(df)
    else:
        st.info("Tarama baslatmak icin butona tiklayin.")


if __name__ == "__main__":
    main()
