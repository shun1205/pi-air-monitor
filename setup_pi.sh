#!/usr/bin/env bash
# Raspberry Pi セットアップスクリプト
# - I2C 有効化
# - InfluxDB 2.x + Grafana インストール
# - Python 依存パッケージ導入
# - air-monitor.service 配置
#
# 使い方: Pi に SCP でこのフォルダを転送し、Pi 上で実行
#   sudo bash setup_pi.sh

set -euo pipefail

# ---- 色付きログ ----
log()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()   { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
die()  { echo -e "\033[1;31m[ERR ]\033[0m  $*" >&2; exit 1; }

# ---- root チェック ----
[ "$(id -u)" -eq 0 ] || die "root で実行してください: sudo bash setup_pi.sh"

# ---- アーキテクチャ確認 ----
ARCH=$(dpkg --print-architecture)
log "アーキテクチャ: ${ARCH}"

PROJECT_DIR="/opt/air-monitor"
TARGET_USER="${SUDO_USER:-pi}"

# ---- 0. 過去の不完全インストールの残骸を掃除 ----
# 古い InfluxData / Grafana の鍵で apt-get update が止まるのを防ぐ
log "古いリポジトリ設定をクリーンアップ..."
rm -f /etc/apt/sources.list.d/influxdata.list
rm -f /etc/apt/sources.list.d/grafana.list
rm -f /etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg
rm -f /etc/apt/trusted.gpg.d/influxdata-archive.gpg
rm -f /etc/apt/trusted.gpg.d/grafana.gpg
ok "クリーンアップ完了"

# ---- 1. APT 更新と必須パッケージ ----
log "APT を更新中..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    i2c-tools \
    python3-pip \
    python3-venv \
    python3-smbus \
    curl \
    gnupg \
    ca-certificates \
    wget
ok "ベースパッケージ導入完了"

# ---- 2. I2C 有効化 ----
log "I2C を有効化..."
if ! grep -q "^dtparam=i2c_arm=on" /boot/firmware/config.txt 2>/dev/null \
  && ! grep -q "^dtparam=i2c_arm=on" /boot/config.txt 2>/dev/null; then
    if [ -f /boot/firmware/config.txt ]; then
        echo "dtparam=i2c_arm=on" >> /boot/firmware/config.txt
    else
        echo "dtparam=i2c_arm=on" >> /boot/config.txt
    fi
    warn "I2C を config.txt に追加 — 再起動後に有効化されます"
fi
# モジュールロード
modprobe i2c-dev || true
if ! grep -q "^i2c-dev" /etc/modules; then
    echo "i2c-dev" >> /etc/modules
fi
# ユーザーを i2c グループに
usermod -aG i2c "${TARGET_USER}" || true
ok "I2C 設定完了"

# ---- 3. InfluxDB 2.x インストール ----
if ! command -v influxd >/dev/null 2>&1; then
    log "InfluxDB 2.x インストール中..."
    # 古い鍵/リポジトリ設定をクリーンアップ
    rm -f /etc/apt/sources.list.d/influxdata.list
    rm -f /etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg
    rm -f /etc/apt/trusted.gpg.d/influxdata-archive.gpg

    # 新しい署名鍵（2024-）を取得して dearmor
    curl -fsSL https://repos.influxdata.com/influxdata-archive.key \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/influxdata-archive.gpg
    chmod 644 /etc/apt/trusted.gpg.d/influxdata-archive.gpg

    # リポジトリ追加
    echo 'deb [signed-by=/etc/apt/trusted.gpg.d/influxdata-archive.gpg] https://repos.influxdata.com/debian stable main' \
        > /etc/apt/sources.list.d/influxdata.list

    apt-get update -qq
    apt-get install -y influxdb2 influxdb2-cli
    systemctl enable --now influxdb
    ok "InfluxDB 2.x 導入完了 (ポート 8086)"
else
    ok "InfluxDB は既にインストール済み"
fi

# ---- 4. Grafana インストール ----
if ! command -v grafana-server >/dev/null 2>&1; then
    log "Grafana インストール中..."
    rm -f /etc/apt/trusted.gpg.d/grafana.gpg
    curl -fsSL https://apt.grafana.com/gpg.key \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/grafana.gpg
    chmod 644 /etc/apt/trusted.gpg.d/grafana.gpg
    echo "deb [signed-by=/etc/apt/trusted.gpg.d/grafana.gpg] https://apt.grafana.com stable main" \
        > /etc/apt/sources.list.d/grafana.list
    apt-get update -qq
    apt-get install -y grafana
    systemctl enable --now grafana-server
    ok "Grafana 導入完了 (ポート 3000)"
else
    ok "Grafana は既にインストール済み"
fi

# ---- 5. Python venv + 依存 ----
log "Python venv 作成..."
mkdir -p "${PROJECT_DIR}"
cp -f collector.py requirements.txt "${PROJECT_DIR}/"
python3 -m venv "${PROJECT_DIR}/venv"
"${PROJECT_DIR}/venv/bin/pip" install --upgrade pip
"${PROJECT_DIR}/venv/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"
chown -R "${TARGET_USER}:${TARGET_USER}" "${PROJECT_DIR}"
ok "Python 依存導入完了"

# ---- 6. systemd サービス配置 ----
log "systemd サービス配置..."
cp -f air-monitor.service /etc/systemd/system/air-monitor.service
systemctl daemon-reload
ok "サービスファイル配置完了"

# ---- 7. I2C 動作確認 ----
log "I2C デバイススキャン..."
if command -v i2cdetect >/dev/null 2>&1; then
    i2cdetect -y 1 || warn "I2C スキャン失敗 — 再起動後にもう一度確認してください"
fi

# ---- 完了メッセージ ----
echo ""
echo "==================================================================="
ok "セットアップ完了！次の手順:"
echo ""
echo "  1) 再起動 (I2C 反映):"
echo "       sudo reboot"
echo ""
echo "  2) 再起動後、配線確認:"
echo "       sudo i2cdetect -y 1"
echo "       → 0x44 (SHT35) と 0x69 (SPS30) が見えること"
echo ""
echo "  3) InfluxDB 初期セットアップ:"
echo "       ブラウザで http://<PiのIP>:8086 を開き"
echo "       Organization='home', Bucket='airquality', "
echo "       ユーザー/パスワードを設定"
echo "       → 発行された API Token を控える"
echo ""
echo "  4) コレクタ設定 (環境変数):"
echo "       sudo nano /etc/default/air-monitor"
echo "       下記を記入:"
echo "         INFLUX_URL=http://localhost:8086"
echo "         INFLUX_ORG=home"
echo "         INFLUX_BUCKET=airquality"
echo "         INFLUX_TOKEN=<手順3の Token>"
echo "         INTERVAL_SEC=10"
echo ""
echo "  5) コレクタ起動:"
echo "       sudo systemctl enable --now air-monitor"
echo "       sudo systemctl status air-monitor"
echo "       sudo journalctl -u air-monitor -f"
echo ""
echo "  6) Grafana セットアップ:"
echo "       ブラウザで http://<PiのIP>:3000 (admin/admin)"
echo "       Data Source に InfluxDB (Flux) を追加 → dashboard.json をインポート"
echo "==================================================================="
