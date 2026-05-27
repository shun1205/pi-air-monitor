#!/usr/bin/env python3
"""
SHT35 + SPS30 → InfluxDB コレクタ

環境変数:
  INFLUX_URL     InfluxDB URL (例: http://localhost:8086)
  INFLUX_ORG     Organization 名
  INFLUX_BUCKET  書き込み先 Bucket
  INFLUX_TOKEN   API Token
  INTERVAL_SEC   サンプリング間隔（秒, デフォルト 10）
  I2C_BUS        I2C バス番号（デフォルト 1）
  LOCATION       location タグ値（デフォルト "room1"）
"""
from __future__ import annotations

import logging
import os
import signal
import struct
import sys
import time
from dataclasses import dataclass
from typing import Optional

from smbus2 import SMBus, i2c_msg
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

# ====================================================================
# 設定
# ====================================================================
INFLUX_URL = os.environ.get("INFLUX_URL", "http://localhost:8086")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "home")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "airquality")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN", "")
INTERVAL_SEC = float(os.environ.get("INTERVAL_SEC", "10"))
I2C_BUS = int(os.environ.get("I2C_BUS", "1"))
LOCATION = os.environ.get("LOCATION", "room1")

SHT35_ADDRS = (0x44, 0x45)   # ADDRピン Low/High どちらでも対応
SPS30_ADDR = 0x69

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("air-monitor")


# ====================================================================
# Sensirion CRC-8 (poly 0x31, init 0xFF) — SHT3x/SPS30 共通
# ====================================================================
def sensirion_crc(data: bytes) -> int:
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc


# ====================================================================
# SHT35 ドライバ
# ====================================================================
def detect_sht35(bus: SMBus) -> int:
    """SHT35 のアドレスを自動検出 (0x44 → 0x45 の順に試す)"""
    for addr in SHT35_ADDRS:
        try:
            bus.i2c_rdwr(i2c_msg.write(addr, [0x24, 0x00]))
            time.sleep(0.020)
            bus.i2c_rdwr(i2c_msg.read(addr, 6))
            return addr
        except OSError:
            continue
    raise IOError(f"SHT35 が見つからない (試したアドレス: {[hex(a) for a in SHT35_ADDRS]})")


class SHT35:
    CMD_MEASURE_HIGH = (0x24, 0x00)

    def __init__(self, bus: SMBus, addr: int):
        self.bus = bus
        self.addr = addr

    def read(self) -> tuple[float, float]:
        # コマンド送信
        write = i2c_msg.write(self.addr, list(self.CMD_MEASURE_HIGH))
        self.bus.i2c_rdwr(write)
        # 測定待ち（高再現性で最大 15ms）
        time.sleep(0.020)
        # 6 byte 読み出し: T_msb, T_lsb, T_crc, RH_msb, RH_lsb, RH_crc
        read = i2c_msg.read(self.addr, 6)
        self.bus.i2c_rdwr(read)
        data = bytes(read)

        if sensirion_crc(data[0:2]) != data[2]:
            raise IOError("SHT35: 温度 CRC エラー")
        if sensirion_crc(data[3:5]) != data[5]:
            raise IOError("SHT35: 湿度 CRC エラー")

        t_raw = (data[0] << 8) | data[1]
        h_raw = (data[3] << 8) | data[4]
        temperature = -45.0 + 175.0 * t_raw / 65535.0
        humidity = 100.0 * h_raw / 65535.0
        return temperature, humidity


# ====================================================================
# SPS30 ドライバ
# ====================================================================
@dataclass
class SPS30Reading:
    pm1_0: float    # μg/m³
    pm2_5: float
    pm4_0: float
    pm10: float
    nc0_5: float    # particle count #/cm³
    nc1_0: float
    nc2_5: float
    nc4_0: float
    nc10: float
    typical_size: float  # μm


class SPS30:
    # 2 バイトコマンド
    CMD_START_MEASUREMENT = (0x00, 0x10)
    CMD_STOP_MEASUREMENT = (0x01, 0x04)
    CMD_READ_DATA_READY = (0x02, 0x02)
    CMD_READ_VALUES = (0x03, 0x00)
    CMD_START_FAN_CLEAN = (0x56, 0x07)
    CMD_RESET = (0xD3, 0x04)

    def __init__(self, bus: SMBus, addr: int = SPS30_ADDR):
        self.bus = bus
        self.addr = addr

    # ---- 低レベルヘルパ ----
    def _send_cmd(self, cmd: tuple[int, int], args: bytes = b"") -> None:
        payload = list(cmd)
        # 引数があれば 2 byte ずつ CRC を追加
        for i in range(0, len(args), 2):
            chunk = args[i : i + 2]
            payload.extend(chunk)
            payload.append(sensirion_crc(chunk))
        msg = i2c_msg.write(self.addr, payload)
        self.bus.i2c_rdwr(msg)

    def _read_bytes(self, n: int) -> bytes:
        msg = i2c_msg.read(self.addr, n)
        self.bus.i2c_rdwr(msg)
        return bytes(msg)

    def _read_words(self, n_words: int) -> bytes:
        """N ワード（各 2 byte + 1 byte CRC）読み、CRC を検証してデータ部のみ返す"""
        raw = self._read_bytes(n_words * 3)
        out = bytearray()
        for i in range(n_words):
            word = raw[i * 3 : i * 3 + 2]
            crc = raw[i * 3 + 2]
            if sensirion_crc(word) != crc:
                raise IOError(f"SPS30: ワード {i} の CRC エラー")
            out += word
        return bytes(out)

    # ---- 公開 API ----
    def start_measurement(self) -> None:
        # データフォーマット: 0x0300 = IEEE 754 float
        self._send_cmd(self.CMD_START_MEASUREMENT, bytes([0x03, 0x00]))
        time.sleep(0.030)

    def stop_measurement(self) -> None:
        self._send_cmd(self.CMD_STOP_MEASUREMENT)
        time.sleep(0.030)

    def reset(self) -> None:
        self._send_cmd(self.CMD_RESET)
        time.sleep(0.100)

    def is_data_ready(self) -> bool:
        self._send_cmd(self.CMD_READ_DATA_READY)
        time.sleep(0.005)
        word = self._read_words(1)
        return word[1] == 0x01

    def read(self, timeout_s: float = 2.0) -> SPS30Reading:
        # データ準備待ち
        deadline = time.monotonic() + timeout_s
        while not self.is_data_ready():
            if time.monotonic() > deadline:
                raise TimeoutError("SPS30: データ準備タイムアウト")
            time.sleep(0.050)

        self._send_cmd(self.CMD_READ_VALUES)
        time.sleep(0.005)
        # 10 個の float = 20 ワード = 60 byte (CRC込み)
        data = self._read_words(20)
        # IEEE 754 big-endian float ×10
        floats = struct.unpack(">10f", data)
        return SPS30Reading(*floats)


# ====================================================================
# メインループ
# ====================================================================
_running = True


def _handle_signal(signum, frame):
    global _running
    log.info("シグナル %d 受信 — 停止します", signum)
    _running = False


def main() -> int:
    if not INFLUX_TOKEN:
        log.error("INFLUX_TOKEN が未設定です (/etc/default/air-monitor を確認)")
        return 1

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info("起動: bus=%d interval=%.1fs location=%s", I2C_BUS, INTERVAL_SEC, LOCATION)

    influx = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    write_api = influx.write_api(write_options=SYNCHRONOUS)

    bus = SMBus(I2C_BUS)

    # SHT35 のアドレス自動検出
    try:
        sht_addr = detect_sht35(bus)
        log.info("SHT35 検出: 0x%02X", sht_addr)
    except Exception as e:
        log.error("SHT35 検出失敗: %s", e)
        bus.close()
        return 3

    sht = SHT35(bus, sht_addr)
    sps = SPS30(bus)

    # SPS30 起動
    try:
        sps.start_measurement()
        log.info("SPS30 測定開始")
    except Exception as e:
        log.exception("SPS30 起動失敗: %s", e)
        return 2

    consecutive_errors = 0
    try:
        while _running:
            loop_start = time.monotonic()
            try:
                temp, humidity = sht.read()
                pm = sps.read()
                ts_ns = time.time_ns()

                point = (
                    Point("air")
                    .tag("location", LOCATION)
                    .field("temperature", float(temp))
                    .field("humidity", float(humidity))
                    .field("pm1_0", float(pm.pm1_0))
                    .field("pm2_5", float(pm.pm2_5))
                    .field("pm4_0", float(pm.pm4_0))
                    .field("pm10", float(pm.pm10))
                    .field("nc0_5", float(pm.nc0_5))
                    .field("nc1_0", float(pm.nc1_0))
                    .field("nc2_5", float(pm.nc2_5))
                    .field("nc4_0", float(pm.nc4_0))
                    .field("nc10", float(pm.nc10))
                    .field("typical_size_um", float(pm.typical_size))
                    .time(ts_ns, WritePrecision.NS)
                )
                write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)
                log.info(
                    "T=%.2f°C RH=%.1f%% PM2.5=%.1fµg/m³ PM10=%.1fµg/m³",
                    temp, humidity, pm.pm2_5, pm.pm10,
                )
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                log.exception("測定/書き込み失敗 (%d回連続): %s", consecutive_errors, e)
                # 連続エラーで SPS30 をリセット
                if consecutive_errors >= 5:
                    log.warning("SPS30 をリセットして再起動します")
                    try:
                        sps.reset()
                        time.sleep(1.0)
                        sps.start_measurement()
                    except Exception:
                        log.exception("SPS30 リセット失敗")
                    consecutive_errors = 0

            elapsed = time.monotonic() - loop_start
            sleep_s = max(0.0, INTERVAL_SEC - elapsed)
            # 細切れに sleep して停止シグナルに早く反応
            slept = 0.0
            while _running and slept < sleep_s:
                step = min(0.5, sleep_s - slept)
                time.sleep(step)
                slept += step
    finally:
        log.info("クリーンアップ中...")
        try:
            sps.stop_measurement()
        except Exception:
            log.exception("SPS30 停止失敗")
        bus.close()
        influx.close()
        log.info("終了")

    return 0


if __name__ == "__main__":
    sys.exit(main())
