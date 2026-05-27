# Pi 側 コマンドチートシート

HDMI モニタ + USB キーボードで Pi を直接操作する際の手順とコマンド集。
このページを **PC のブラウザで開いておき**、Pi のキーボードで打ち込みながら参照する想定。

URL: https://github.com/shun1205/pi-air-monitor/blob/main/PI_COMMANDS.md

---

## 0. 前提

- Pi 4 + Raspberry Pi OS (Bookworm)
- スマホテザリング (POCO X7 Pro) の WiFi に Pi が接続
- 配線済み（SHT35 → 0x44, SPS30 → 0x69）

---

## 1. ネットワーク確認

### 1-1. WiFi 接続状態

```bash
ip -4 addr show wlan0
nmcli connection show --active
```

`inet 10.xxx.xxx.xxx/24` のような行が wlan0 にあれば WiFi 接続OK。

### 1-2. インターネット疎通

```bash
ping -c 2 8.8.8.8       # IP疎通（DNSなし）
ping -c 2 github.com    # DNS解決＋疎通
```

### 1-3. 接続できていない場合の再接続

```bash
# スマホで WiFi テザリングを再度 ON にしてから:
sudo nmcli device wifi rescan
sudo nmcli device wifi connect "POCO X7 Pro" password "wts52v2tvw29z7v"
ip -4 addr show wlan0
ping -c 2 8.8.8.8
```

### 1-4. DNS だけが解決できないとき

```bash
# 一時的に Google DNS を使う
sudo sh -c 'echo "nameserver 8.8.8.8" >> /etc/resolv.conf'
ping -c 2 github.com
```

---

## 2. プロジェクト取得

```bash
cd ~
git clone https://github.com/shun1205/pi-air-monitor.git
cd pi-air-monitor
ls -la
```

ファイルが 10 個ほど見えれば取得成功。

---

## 3. I2C 有効化と配線確認

```bash
# I2C 有効化（initial setup の一部、setup_pi.sh でもやるが先に確認するため）
sudo apt update
sudo apt install -y i2c-tools python3-smbus
sudo raspi-config nonint do_i2c 0
sudo reboot
```

再起動後、再ログインして：

```bash
sudo i2cdetect -y 1
```

期待される出力：

```
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:                         -- -- -- -- -- -- -- --
10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
20: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
30: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
40: -- -- -- -- 44 -- -- -- -- -- -- -- -- -- -- --   ← SHT35
50: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
60: -- -- -- -- -- -- -- -- -- 69 -- -- -- -- -- --   ← SPS30
70: -- -- -- -- -- -- -- --
```

`44` と `69` 両方見えればOK。見えない場合は WIRING.md を再確認。

---

## 4. センサ単独テスト（InfluxDB なしで疎通確認）

```bash
cd ~/pi-air-monitor
pip3 install --break-system-packages smbus2
python3 test_sensors.py
```

温度・湿度・PM 値が 10 回コンソールに流れれば OK。

---

## 5. フルセットアップ（InfluxDB + Grafana + collector 起動）

```bash
cd ~/pi-air-monitor
sudo bash setup_pi.sh
```

完了後の手順（スクリプト末尾にも表示される）：

### 5-1. InfluxDB 初期化

PC のブラウザで `http://<PiのIP>:8086` を開く。
- Username/Password: 任意（覚えておく）
- Organization: **home**
- Bucket: **airquality**
- Continue → API Token をコピー

> ⚠️ PC とPi が同じネットワークでないとブラウザで開けない。
> その場合は Pi 上の `chromium-browser http://localhost:8086` で開く（HDMI モニタ必要）。

### 5-2. 環境変数設定

```bash
sudo cp /home/kajima1205/pi-air-monitor/air-monitor.env.example /etc/default/air-monitor
sudo nano /etc/default/air-monitor
```

`INFLUX_TOKEN=` に取得したトークンを貼り付けて保存（Ctrl+O, Enter, Ctrl+X）。

### 5-3. コレクタ起動

```bash
sudo systemctl enable --now air-monitor
sudo systemctl status air-monitor
sudo journalctl -u air-monitor -f
```

`T=23.45°C RH=54.2% PM2.5=3.1µg/m³ ...` のようなログが 10 秒ごとに出れば成功。

### 5-4. Grafana セットアップ

PC のブラウザで `http://<PiのIP>:3000`（admin/admin、要パスワード変更）

詳細手順は [README.md](./README.md#7-grafana-セットアップ) の「7. Grafana セットアップ」を参照。

---

## 6. トラブルシューティング

### Pi の IP を忘れた

```bash
hostname -I
ip -4 addr show wlan0
```

### コレクタが起動しない

```bash
sudo journalctl -u air-monitor -n 50 --no-pager
```

エラー内容を確認。よくある原因：
- `INFLUX_TOKEN が未設定` → `/etc/default/air-monitor` のトークン未設定
- `SPS30 起動失敗` → 配線（特に Pin4 SEL → GND）
- `i2c-1 Permission denied` → ユーザーが i2c グループにいない → `sudo usermod -aG i2c $USER` 後再ログイン

### サービス停止 / 再起動

```bash
sudo systemctl stop air-monitor       # 停止
sudo systemctl restart air-monitor    # 再起動
sudo systemctl disable air-monitor    # 自動起動解除
```

### 古いログを削除（容量逼迫時）

```bash
sudo journalctl --vacuum-time=7d      # 7日以上前のログ削除
```

---

## 7. SSH で繋ぐ場合（PC ⇔ Pi 同一サブネット時）

```bash
# Pi 側で IP 確認
hostname -I

# Pi 側で SSH 有効化（既にやってあるはず）
sudo systemctl enable --now ssh

# PC 側から
ssh kajima1205@<PiのIP>
```

> 💡 スマホテザリングは AP Isolation で PC ⇔ Pi 通信が遮断されていることが多い。
> 自宅 Buffalo-A-A770 など通常のルーター配下なら問題なく繋がる。

---

## 8. 撤収 / 再セットアップ

```bash
sudo systemctl disable --now air-monitor
sudo systemctl disable --now influxdb
sudo systemctl disable --now grafana-server
sudo rm -rf /opt/air-monitor
sudo rm /etc/systemd/system/air-monitor.service
sudo rm /etc/default/air-monitor
# 完全撤去:
sudo apt purge -y influxdb2 grafana
```
=== センサ疎通テスト (I2C bus 1) ===

[1/3] SHT35 (0x44) を読みます...
      NG  [Errno 5] Input/output error
           → i2cdetect -y 1 で 0x44 が見えるか確認

[2/3] SPS30 (0x69) を起動します...
      OK  測定開始（ファンが回り始めるはず）

[3/3] SPS30 を 10 回連続読み出し (約 10 秒)...
      （初回 5-10 秒はウォームアップで値が安定しない）

        # |      T |    RH |  PM1.0 |  PM2.5 |  PM4.0 |   PM10 | size
      ------------------------------------------------------------------------------

      NG  読み出し中にエラー: [Errno 5] Input/output error

=== テスト完了 ===
