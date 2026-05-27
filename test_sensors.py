#!/usr/bin/env python3
"""
センサ疎通テスト — InfluxDB なしで SHT35 と SPS30 を読み、コンソールに表示する。
collector.py と同じドライバを再利用。

使い方 (Pi 上で):
    sudo apt install -y python3-smbus3 python3-pip
    pip3 install --break-system-packages smbus2
    python3 test_sensors.py
"""
from __future__ import annotations

import struct
import sys
import time
from dataclasses import dataclass

from smbus2 import SMBus, i2c_msg

SHT35_ADDRS = (0x44, 0x45)   # ADDRピンが Low/High どちらでも対応
SPS30_ADDR = 0x69
I2C_BUS = 1


def detect_sht35(bus) -> int:
    """0x44 / 0x45 のうち応答があったアドレスを返す。見つからなければ例外。"""
    for addr in SHT35_ADDRS:
        try:
            bus.i2c_rdwr(i2c_msg.write(addr, [0x24, 0x00]))
            time.sleep(0.020)
            msg = i2c_msg.read(addr, 6)
            bus.i2c_rdwr(msg)
            return addr
        except OSError:
            continue
    raise IOError(f"SHT35 が見つからない (試したアドレス: {[hex(a) for a in SHT35_ADDRS]})")


def sensirion_crc(data: bytes) -> int:
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc


# ---- SHT35 ----
class SHT35:
    CMD_MEASURE_HIGH = (0x24, 0x00)

    def __init__(self, bus, addr):
        self.bus = bus
        self.addr = addr

    def read(self):
        self.bus.i2c_rdwr(i2c_msg.write(self.addr, list(self.CMD_MEASURE_HIGH)))
        time.sleep(0.020)
        msg = i2c_msg.read(self.addr, 6)
        self.bus.i2c_rdwr(msg)
        data = bytes(msg)
        if sensirion_crc(data[0:2]) != data[2]:
            raise IOError("SHT35 温度 CRC エラー")
        if sensirion_crc(data[3:5]) != data[5]:
            raise IOError("SHT35 湿度 CRC エラー")
        t_raw = (data[0] << 8) | data[1]
        h_raw = (data[3] << 8) | data[4]
        return -45.0 + 175.0 * t_raw / 65535.0, 100.0 * h_raw / 65535.0


# ---- SPS30 ----
@dataclass
class SPS30Reading:
    pm1_0: float
    pm2_5: float
    pm4_0: float
    pm10: float
    nc0_5: float
    nc1_0: float
    nc2_5: float
    nc4_0: float
    nc10: float
    typical_size: float


class SPS30:
    CMD_START = (0x00, 0x10)
    CMD_STOP = (0x01, 0x04)
    CMD_READY = (0x02, 0x02)
    CMD_READ = (0x03, 0x00)

    def __init__(self, bus, addr=SPS30_ADDR):
        self.bus = bus
        self.addr = addr

    def _send(self, cmd, args=b""):
        payload = list(cmd)
        for i in range(0, len(args), 2):
            chunk = args[i:i+2]
            payload.extend(chunk)
            payload.append(sensirion_crc(chunk))
        self.bus.i2c_rdwr(i2c_msg.write(self.addr, payload))

    def _read_words(self, n):
        msg = i2c_msg.read(self.addr, n * 3)
        self.bus.i2c_rdwr(msg)
        raw = bytes(msg)
        out = bytearray()
        for i in range(n):
            word = raw[i*3:i*3+2]
            if sensirion_crc(word) != raw[i*3+2]:
                raise IOError(f"SPS30 ワード {i} CRC エラー")
            out += word
        return bytes(out)

    def start(self):
        self._send(self.CMD_START, bytes([0x03, 0x00]))
        time.sleep(0.030)

    def stop(self):
        self._send(self.CMD_STOP)
        time.sleep(0.030)

    def ready(self):
        self._send(self.CMD_READY)
        time.sleep(0.005)
        return self._read_words(1)[1] == 0x01

    def read(self, timeout=3.0):
        deadline = time.monotonic() + timeout
        while not self.ready():
            if time.monotonic() > deadline:
                raise TimeoutError("SPS30 データ準備タイムアウト")
            time.sleep(0.1)
        self._send(self.CMD_READ)
        time.sleep(0.005)
        return SPS30Reading(*struct.unpack(">10f", self._read_words(20)))


def main():
    print(f"=== センサ疎通テスト (I2C bus {I2C_BUS}) ===\n")

    try:
        bus = SMBus(I2C_BUS)
    except Exception as e:
        print(f"[NG] I2C バス {I2C_BUS} を開けません: {e}")
        print("    → raspi-config で I2C を有効化、再起動を確認")
        return 1

    # --- SHT35 アドレス検出＋単発テスト ---
    print("[1/3] SHT35 (0x44/0x45 自動検出) を読みます...")
    sht = None
    try:
        addr = detect_sht35(bus)
        print(f"      アドレス検出: 0x{addr:02X}")
        sht = SHT35(bus, addr)
        t, h = sht.read()
        print(f"      OK  温度 {t:.2f}°C  湿度 {h:.1f}%RH\n")
    except Exception as e:
        print(f"      NG  {e}")
        print(f"           → i2cdetect -y 1 で 0x44 か 0x45 が見えるか確認\n")

    # --- SPS30 起動 ---
    print("[2/3] SPS30 (0x69) を起動します...")
    sps = SPS30(bus)
    try:
        sps.start()
        print("      OK  測定開始（ファンが回り始めるはず）\n")
    except Exception as e:
        print(f"      NG  {e}")
        print("           → SPS30 の Pin4 SEL が GND に接続されているか確認")
        print("           → VDD が 5V か確認")
        bus.close()
        return 2

    # --- SPS30 読み出し（10回） ---
    print("[3/3] SPS30 を 10 回連続読み出し (約 10 秒)...")
    print("      （初回 5-10 秒はウォームアップで値が安定しない）\n")
    print(f"      {'#':>3} | {'T':>6} | {'RH':>5} | {'PM1.0':>6} | {'PM2.5':>6} | {'PM4.0':>6} | {'PM10':>6} | size")
    print("      " + "-" * 78)
    for i in range(10):
        try:
            t_str = f"{sht.read()[0]:>5.2f}°" if sht else "  --- "
            h_str = f"{sht.read()[1]:>4.1f}%" if sht else " --- "
        except Exception as e:
            t_str, h_str = "  ERR ", " ERR "
        try:
            pm = sps.read()
            pm_line = (f"{pm.pm1_0:>6.2f} | {pm.pm2_5:>6.2f} | "
                       f"{pm.pm4_0:>6.2f} | {pm.pm10:>6.2f} | {pm.typical_size:.2f}µm")
        except Exception as e:
            pm_line = f"SPS30 read エラー: {e}"
        print(f"      {i+1:>3} | {t_str} | {h_str} | {pm_line}")
        try:
            time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n      中断")
            break

    try:
        sps.stop()
    except Exception:
        pass
    bus.close()

    print("\n=== テスト完了 ===")
    print("値が妥当そうなら collector.py 本番運用に進めます。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
