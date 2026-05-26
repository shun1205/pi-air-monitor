# Raspberry Pi 空気質モニター

Raspberry Pi + Grove SHT35 (温湿度) + Sensirion SPS30 (PM1.0/2.5/4.0/10) で計測し、
InfluxDB に蓄積、Grafana でリアルタイム閲覧する一式。

## ファイル一覧

| ファイル | 用途 |
|---|---|
| [WIRING.md](./WIRING.md) | 配線図とピン配置 |
| [setup_pi.sh](./setup_pi.sh) | Pi 上で実行するセットアップスクリプト |
| [collector.py](./collector.py) | センサ読み取り → InfluxDB 書き込み常駐スクリプト |
| [requirements.txt](./requirements.txt) | Python 依存パッケージ |
| [air-monitor.service](./air-monitor.service) | systemd サービス定義 |
| [air-monitor.env.example](./air-monitor.env.example) | 環境変数テンプレート |
| [grafana_dashboard.json](./grafana_dashboard.json) | Grafana ダッシュボード（インポート用） |

---

## 全体の流れ

1. **配線** — [WIRING.md](./WIRING.md) に従って SHT35 と SPS30 を Pi の GPIO に接続
2. **Pi に転送** — このフォルダを Pi にコピー
3. **セットアップ実行** — `sudo bash setup_pi.sh` で InfluxDB + Grafana + Python 環境一括導入
4. **再起動** — I2C を反映
5. **InfluxDB 初期化** — Web UI で Org/Bucket/Token を作成
6. **環境変数記入** — `/etc/default/air-monitor` に Token を貼り付け
7. **コレクタ起動** — `sudo systemctl enable --now air-monitor`
8. **Grafana ダッシュボード** — Web UI でデータソース追加 → ダッシュボード JSON インポート
9. **PC から閲覧** — `http://<PiのIP>:3000`

---

## ステップ詳細

### 1. ファイル転送（PC → Pi）

Pi の IP アドレスを先に確認しておきます（Pi の画面で `hostname -I`、または Pi Imager で設定したホスト名 `<host>.local`）。

PowerShell またはターミナルから:

```bash
# このフォルダから Pi へ転送（ユーザー名・IP は実際のものに置き換え）
scp -r c:/Users/kakou01/Desktop/vscode/pi-air-monitor pi@<PiのIP>:~/
```

または USB メモリで `/home/pi/pi-air-monitor/` にコピー。

### 2. Pi にログインしてセットアップ

```bash
ssh pi@<PiのIP>
cd ~/pi-air-monitor
sudo bash setup_pi.sh
sudo reboot
```

### 3. 配線確認

再起動後、Pi にログインして:

```bash
sudo i2cdetect -y 1
```

`0x44` (SHT35) と `0x69` (SPS30) の両方が表示されること。
見えない場合は [WIRING.md](./WIRING.md) を再確認。
特に **SPS30 の SEL ピンが GND に落ちているか**、**SPS30 の VDD が 5V か** を要チェック。

### 4. InfluxDB 初期化

PC のブラウザから `http://<PiのIP>:8086` を開く。
初回起動画面で以下を入力:

- Username / Password: 任意（覚えておく）
- Initial Organization Name: **`home`**
- Initial Bucket Name: **`airquality`**

「Continue」後に表示される **API Token を必ずコピー**。

### 5. コレクタの環境変数を設定

```bash
sudo cp /home/pi/pi-air-monitor/air-monitor.env.example /etc/default/air-monitor
sudo nano /etc/default/air-monitor
```

`INFLUX_TOKEN=` に手順 4 でコピーしたトークンを貼り付け、保存。

### 6. コレクタ起動

```bash
sudo systemctl enable --now air-monitor
sudo systemctl status air-monitor      # active (running) を確認
sudo journalctl -u air-monitor -f      # ログ追跡（Ctrl+C で抜ける）
```

ログに `T=23.45°C RH=54.2% PM2.5=3.1µg/m³ PM10=4.8µg/m³` のような行が
10 秒ごとに出ていれば成功。

### 7. Grafana セットアップ

PC のブラウザから `http://<PiのIP>:3000`（初期 admin/admin、要パスワード変更）

**データソース追加:**
1. 左メニュー「Connections」→「Data sources」→「Add data source」→「InfluxDB」
2. Query Language: **Flux**
3. URL: `http://localhost:8086`
4. Organization: `home`
5. Token: 手順 4 で取得したトークン
6. Default Bucket: `airquality`
7. 「Save & test」→ 成功表示

**ダッシュボードインポート:**
1. 左メニュー「Dashboards」→「New」→「Import」
2. `grafana_dashboard.json` の中身を貼り付け、または upload
3. Variable `DS_INFLUXDB` に先ほど作った InfluxDB を選択
4. 「Import」

これで PC のブラウザで温湿度・PM 値がリアルタイムに表示されます。
右上の Time range を「Last 5 minutes」「Last 24 hours」など切り替えて確認。

### 8. ログ保持期間の設定（任意）

数ヶ月分の保存が目的なので、InfluxDB の Bucket 設定で Retention を変更:

```bash
# 例: 180 日保持
influx bucket update --name airquality --retention 180d -t <YOUR_TOKEN>
```

または Web UI「Data → Buckets → airquality → Settings」から変更可。

---

## トラブルシューティング

### `i2cdetect` でアドレスが見えない

- **SHT35 (0x44)** が見えない → ジャンパーワイヤの接触、SDA/SCL の逆挿し、3.3V 供給を確認
- **SPS30 (0x69)** が見えない →
  - **Pin 4 (SEL) が GND に接続されているか** が最重要
  - VDD が **5V** か（3.3V では起動しない）
  - 電源投入し直し（SEL は電源投入時に判定される）

### コレクタが `INFLUX_TOKEN が未設定` でエラー

`/etc/default/air-monitor` の編集後、サービス再起動:
```bash
sudo systemctl restart air-monitor
```

### Grafana にデータが出ない

- コレクタログを確認: `sudo journalctl -u air-monitor -n 50`
- InfluxDB の Web UI「Data Explorer」で `airquality` バケットを直接クエリしてみる
- データソース設定で Token と Bucket 名が一致しているか確認

### 一時的に止めたい

```bash
sudo systemctl stop air-monitor
```

### SPS30 の自動ファンクリーニング

SPS30 は週 1 回ファンを自動清掃する設計だが、本コレクタでは未呼び出し。
必要なら `collector.py` 内で `sps._send_cmd(SPS30.CMD_START_FAN_CLEAN)` を週次で呼ぶか、
cron に登録すること（推奨ではあるが必須ではない）。

---

## アーキテクチャ概要

```
 ┌───────────┐  I2C   ┌──────────────┐  HTTP   ┌──────────┐
 │  SHT35    │───────►│              │────────►│ InfluxDB │
 ├───────────┤        │ collector.py │         │  :8086   │
 │  SPS30    │───────►│  (systemd)   │         └────┬─────┘
 └───────────┘        └──────────────┘              │ Flux
                                                    ▼
                                          ┌──────────────────┐  HTTP
                                          │ Grafana (:3000)  │◄──── PC ブラウザ
                                          └──────────────────┘
```

すべて Pi 1 台で完結。外部サービス不要、ネットなしでも動作（ただし時刻同期は推奨）。
