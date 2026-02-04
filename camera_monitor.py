#!/usr/bin/env python3
"""Monitor IP cameras listed in a CSV file.

CSV headers: IP, RTSP_URL, Lokasyon
Outputs a timestamped CSV report and prints a summary table.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import dataclass
from datetime import datetime
import platform
import subprocess
from typing import Iterable, List, Optional, Tuple

import cv2
import numpy as np
import smtplib
import urllib.request
import urllib.parse
from email.message import EmailMessage
import importlib


@dataclass(frozen=True)
class Camera:
    ip: str
    rtsp_url: str
    location: str


@dataclass(frozen=True)
class CameraResult:
    camera: Camera
    status: str
    ping_ms: Optional[float]
    detail: str
    health: str
    health_detail: str
    alert_sent: bool
    alert_detail: str


PING_TIMEOUT_S = 1
BLUR_THRESHOLD = 100.0
FREEZE_THRESHOLD = 2.0
BLACK_THRESHOLD = 15.0


@dataclass(frozen=True)
class AlertConfig:
    telegram_token: str
    telegram_chat_id: str
    email_to: str
    email_from: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    alert_on_health: bool


@dataclass(frozen=True)
class SnmpResetConfig:
    enabled: bool
    host: str
    port: int
    community: str
    ifindex: int


def load_cameras(csv_path: str) -> List[Camera]:
    cameras: List[Camera] = []
    with open(csv_path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = {"IP", "RTSP_URL", "Lokasyon"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV eksik kolonlar: {', '.join(sorted(missing))}")
        for row in reader:
            ip = (row.get("IP") or "").strip()
            rtsp_url = (row.get("RTSP_URL") or "").strip()
            location = (row.get("Lokasyon") or "").strip()
            if not ip or not rtsp_url:
                continue
            cameras.append(Camera(ip=ip, rtsp_url=rtsp_url, location=location))
    return cameras


def ping_host(ip: str, timeout_s: int = PING_TIMEOUT_S) -> tuple[bool, Optional[float], str]:
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout_s * 1000), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(timeout_s), ip]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return False, None, "ping komutu bulunamadı"

    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        return False, None, "ping yanıtı alınamadı"

    latency = None
    for token in output.split():
        if token.startswith("time="):
            try:
                latency = float(token.split("=")[1])
            except ValueError:
                latency = None
            break
    return True, latency, "ping başarılı"


def analyze_frames(frame_a: np.ndarray, frame_b: np.ndarray) -> Tuple[str, str]:
    gray_a = cv2.cvtColor(frame_a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(frame_b, cv2.COLOR_BGR2GRAY)

    blur_score = cv2.Laplacian(gray_a, cv2.CV_64F).var()
    blur_issue = blur_score < BLUR_THRESHOLD

    diff = gray_a.astype("float32") - gray_b.astype("float32")
    mse = np.mean(diff ** 2)
    freeze_issue = mse < FREEZE_THRESHOLD

    brightness = float(np.mean(gray_a))
    black_issue = brightness < BLACK_THRESHOLD

    issues: List[str] = []
    if blur_issue:
        issues.append(f"Bulaniklik (Laplacian={blur_score:.1f})")
    if freeze_issue:
        issues.append(f"Donma (MSE={mse:.2f})")
    if black_issue:
        issues.append(f"Siyah Ekran (Parlaklik={brightness:.1f})")

    if issues:
        return "Sorunlu", "; ".join(issues)
    return "Saglikli", f"Blur={blur_score:.1f}, MSE={mse:.2f}, Parlaklik={brightness:.1f}"


def build_alert_message(result: CameraResult) -> str:
    return (
        "Kamera uyarisi:\n"
        f"IP: {result.camera.ip}\n"
        f"Lokasyon: {result.camera.location}\n"
        f"Durum: {result.status}\n"
        f"Detay: {result.detail}\n"
        f"Saglik: {result.health}\n"
        f"Saglik Detay: {result.health_detail}\n"
    )


def send_telegram_alert(message: str, config: AlertConfig) -> Tuple[bool, str]:
    if not config.telegram_token or not config.telegram_chat_id:
        return False, "Telegram ayarlari eksik"
    url = f"https://api.telegram.org/bot{config.telegram_token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": config.telegram_chat_id, "text": message}).encode("utf-8")
    try:
        with urllib.request.urlopen(url, data=payload, timeout=10) as response:
            if response.status >= 400:
                return False, f"Telegram hata kodu: {response.status}"
    except Exception as exc:
        return False, f"Telegram gonderim hatasi: {exc}"
    return True, "Telegram gonderildi"


def send_email_alert(message: str, config: AlertConfig) -> Tuple[bool, str]:
    if not config.email_to or not config.smtp_host or not config.email_from:
        return False, "E-posta ayarlari eksik"
    email = EmailMessage()
    email["Subject"] = "Kamera Uyarisi"
    email["From"] = config.email_from
    email["To"] = config.email_to
    email.set_content(message)
    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10) as server:
            server.starttls()
            if config.smtp_user:
                server.login(config.smtp_user, config.smtp_password)
            server.send_message(email)
    except Exception as exc:
        return False, f"E-posta gonderim hatasi: {exc}"
    return True, "E-posta gonderildi"


def maybe_reset_snmp_port(config: SnmpResetConfig) -> Tuple[bool, str]:
    if not config.enabled:
        return False, "SNMP reset kapali"
    if not config.host or not config.community or not config.ifindex:
        return False, "SNMP ayarlari eksik"
    try:
        pysnmp = importlib.import_module("pysnmp.hlapi")
    except ModuleNotFoundError:
        return False, "pysnmp kurulu degil"
    snmp_engine = pysnmp.SnmpEngine()
    community_data = pysnmp.CommunityData(config.community, mpModel=1)
    transport = pysnmp.UdpTransportTarget((config.host, config.port), timeout=2, retries=1)
    context = pysnmp.ContextData()
    if_admin_status = pysnmp.ObjectType(pysnmp.ObjectIdentity("1.3.6.1.2.1.2.2.1.7", config.ifindex), 2)
    if_admin_status_up = pysnmp.ObjectType(pysnmp.ObjectIdentity("1.3.6.1.2.1.2.2.1.7", config.ifindex), 1)

    try:
        for error_indication, error_status, error_index, _var_binds in pysnmp.setCmd(
            snmp_engine, community_data, transport, context, if_admin_status
        ):
            if error_indication:
                return False, f"SNMP hata: {error_indication}"
            if error_status:
                return False, f"SNMP hata: {error_status.prettyPrint()}@{error_index}"
        for error_indication, error_status, error_index, _var_binds in pysnmp.setCmd(
            snmp_engine, community_data, transport, context, if_admin_status_up
        ):
            if error_indication:
                return False, f"SNMP hata: {error_indication}"
            if error_status:
                return False, f"SNMP hata: {error_status.prettyPrint()}@{error_index}"
    except Exception as exc:
        return False, f"SNMP reset hatasi: {exc}"
    return True, "SNMP port resetlendi"


def check_stream_sync(rtsp_url: str, read_timeout_s: float) -> tuple[bool, str, str, str]:
    cap = cv2.VideoCapture(rtsp_url)
    try:
        if not cap.isOpened():
            return False, "RTSP bağlantısı açılamadı", "Bilinmiyor", "RTSP baglantisi acilamadi"
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        start = datetime.now()
        first_frame = None
        while (datetime.now() - start).total_seconds() < read_timeout_s:
            ok, frame = cap.read()
            if not ok:
                continue
            if first_frame is None:
                first_frame = frame
                continue
            health, health_detail = analyze_frames(first_frame, frame)
            return True, "RTSP yayın aktif", health, health_detail
        return False, "RTSP yayınında frame alınamadı", "Bilinmiyor", "Frame alinmadi"
    finally:
        cap.release()


async def check_camera(
    camera: Camera,
    stream_timeout_s: float,
    alert_config: AlertConfig,
    snmp_config: SnmpResetConfig,
) -> CameraResult:
    ping_ok, ping_ms, ping_detail = await asyncio.to_thread(ping_host, camera.ip)
    if not ping_ok:
        result = CameraResult(
            camera=camera,
            status="Erişilemiyor",
            ping_ms=ping_ms,
            detail=ping_detail,
            health="Bilinmiyor",
            health_detail="Ping basarisiz",
            alert_sent=False,
            alert_detail="Uyari gonderilmedi",
        )
        return await handle_alerts(result, alert_config, snmp_config)

    try:
        stream_ok, stream_detail, health, health_detail = await asyncio.wait_for(
            asyncio.to_thread(check_stream_sync, camera.rtsp_url, stream_timeout_s),
            timeout=stream_timeout_s + 1,
        )
    except asyncio.TimeoutError:
        result = CameraResult(
            camera=camera,
            status="Yayın Yok",
            ping_ms=ping_ms,
            detail="RTSP zaman aşımı",
            health="Bilinmiyor",
            health_detail="RTSP zaman asimi",
            alert_sent=False,
            alert_detail="Uyari gonderilmedi",
        )
        return await handle_alerts(result, alert_config, snmp_config)

    if stream_ok:
        result = CameraResult(
            camera=camera,
            status="Aktif",
            ping_ms=ping_ms,
            detail=stream_detail,
            health=health,
            health_detail=health_detail,
            alert_sent=False,
            alert_detail="Uyari gonderilmedi",
        )
        return await handle_alerts(result, alert_config, snmp_config)
    result = CameraResult(
        camera=camera,
        status="Yayın Yok",
        ping_ms=ping_ms,
        detail=stream_detail,
        health=health,
        health_detail=health_detail,
        alert_sent=False,
        alert_detail="Uyari gonderilmedi",
    )
    return await handle_alerts(result, alert_config, snmp_config)


async def handle_alerts(
    result: CameraResult,
    alert_config: AlertConfig,
    snmp_config: SnmpResetConfig,
) -> CameraResult:
    should_alert = result.status != "Aktif" or (alert_config.alert_on_health and result.health == "Sorunlu")
    if not should_alert:
        return result
    message = build_alert_message(result)
    telegram_ok, telegram_detail = await asyncio.to_thread(send_telegram_alert, message, alert_config)
    email_ok, email_detail = await asyncio.to_thread(send_email_alert, message, alert_config)
    snmp_ok, snmp_detail = await asyncio.to_thread(maybe_reset_snmp_port, snmp_config)

    alert_details = [telegram_detail, email_detail, snmp_detail]
    return CameraResult(
        camera=result.camera,
        status=result.status,
        ping_ms=result.ping_ms,
        detail=result.detail,
        health=result.health,
        health_detail=result.health_detail,
        alert_sent=telegram_ok or email_ok or snmp_ok,
        alert_detail="; ".join(alert_details),
    )


async def run_checks(
    cameras: Iterable[Camera],
    stream_timeout_s: float,
    concurrency: int,
    alert_config: AlertConfig,
    snmp_config: SnmpResetConfig,
) -> List[CameraResult]:
    semaphore = asyncio.Semaphore(concurrency)

    async def _wrapped(camera: Camera) -> CameraResult:
        async with semaphore:
            return await check_camera(camera, stream_timeout_s, alert_config, snmp_config)

    tasks = [asyncio.create_task(_wrapped(camera)) for camera in cameras]
    return await asyncio.gather(*tasks)


def write_report(results: Iterable[CameraResult], output_path: str) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "IP",
                "RTSP_URL",
                "Lokasyon",
                "Durum",
                "Ping_ms",
                "Detay",
                "Saglik",
                "Saglik_Detay",
                "Uyari",
                "Uyari_Detay",
            ]
        )
        for result in results:
            writer.writerow(
                [
                    result.camera.ip,
                    result.camera.rtsp_url,
                    result.camera.location,
                    result.status,
                    "" if result.ping_ms is None else f"{result.ping_ms:.2f}",
                    result.detail,
                    result.health,
                    result.health_detail,
                    "Evet" if result.alert_sent else "Hayir",
                    result.alert_detail,
                ]
            )


def print_summary(results: Iterable[CameraResult]) -> None:
    print("\n--- Kamera Durum Ozeti ---")
    for result in results:
        ping_display = "-" if result.ping_ms is None else f"{result.ping_ms:.1f}ms"
        print(
            f"{result.camera.ip:15} | {result.camera.location:20} | {result.status:12} | "
            f"{result.health:9} | ping: {ping_display} | uyari: {'Evet' if result.alert_sent else 'Hayir'}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IP kamera izleme araci")
    parser.add_argument("--csv", required=True, help="Kamera listesi CSV yolu")
    parser.add_argument("--stream-timeout", type=float, default=4.0, help="RTSP frame bekleme suresi (s)")
    parser.add_argument("--concurrency", type=int, default=50, help="Ayni anda kontrol edilen kamera sayisi")
    parser.add_argument(
        "--blur-threshold",
        type=float,
        default=100.0,
        help="Laplacian blur esigi (dusukse bulaniklik kabul edilir)",
    )
    parser.add_argument(
        "--freeze-threshold",
        type=float,
        default=2.0,
        help="Frame MSE esigi (dusukse donma kabul edilir)",
    )
    parser.add_argument(
        "--black-threshold",
        type=float,
        default=15.0,
        help="Ortalama parlaklik esigi (dusukse siyah ekran kabul edilir)",
    )
    parser.add_argument("--alert-on-health", action="store_true", help="Saglik sorunlarinda da uyari gonder")
    parser.add_argument("--telegram-token", default="", help="Telegram bot token")
    parser.add_argument("--telegram-chat-id", default="", help="Telegram grup veya kanal ID")
    parser.add_argument("--email-to", default="", help="Uyari e-posta alicisi")
    parser.add_argument("--email-from", default="", help="Uyari e-posta gonderen")
    parser.add_argument("--smtp-host", default="", help="SMTP sunucu adresi")
    parser.add_argument("--smtp-port", type=int, default=587, help="SMTP port")
    parser.add_argument("--smtp-user", default="", help="SMTP kullanici adi")
    parser.add_argument("--smtp-pass", default="", help="SMTP sifresi")
    parser.add_argument("--snmp-reset", action="store_true", help="SNMP ile port resetlemeyi dene")
    parser.add_argument("--snmp-host", default="", help="Switch SNMP host")
    parser.add_argument("--snmp-port", type=int, default=161, help="Switch SNMP port")
    parser.add_argument("--snmp-community", default="", help="SNMP community")
    parser.add_argument("--snmp-ifindex", type=int, default=0, help="Resetlenecek port ifIndex")
    parser.add_argument("--output", default="", help="Rapor CSV dosya yolu")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    global BLUR_THRESHOLD, FREEZE_THRESHOLD, BLACK_THRESHOLD
    BLUR_THRESHOLD = args.blur_threshold
    FREEZE_THRESHOLD = args.freeze_threshold
    BLACK_THRESHOLD = args.black_threshold
    alert_config = AlertConfig(
        telegram_token=args.telegram_token,
        telegram_chat_id=args.telegram_chat_id,
        email_to=args.email_to,
        email_from=args.email_from,
        smtp_host=args.smtp_host,
        smtp_port=args.smtp_port,
        smtp_user=args.smtp_user,
        smtp_password=args.smtp_pass,
        alert_on_health=args.alert_on_health,
    )
    snmp_config = SnmpResetConfig(
        enabled=args.snmp_reset,
        host=args.snmp_host,
        port=args.snmp_port,
        community=args.snmp_community,
        ifindex=args.snmp_ifindex,
    )
    cameras = load_cameras(args.csv)
    if not cameras:
        raise SystemExit("CSV icinde kamera bulunamadi")

    output_path = args.output
    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"kamera_raporu_{timestamp}.csv"

    results = asyncio.run(run_checks(cameras, args.stream_timeout, args.concurrency, alert_config, snmp_config))
    write_report(results, output_path)
    print_summary(results)
    print(f"\nRapor yazildi: {output_path}")


if __name__ == "__main__":
    main()
