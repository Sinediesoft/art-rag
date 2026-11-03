#!/usr/bin/env bash
# 完整版離線包（docs/adr/046）的 WSL 環境：deploy/offline/pack.ps1 在一個暫時匯入的 Ubuntu 24.04 裡以 root 執行，
# 做好之後整個環境匯出成 offline/wsl/artrag.tar，裝的電腦用 wsl --import 匯入，不連網。
# 取代 Docker：PostgreSQL＋pgvector、Redis 都跑在這裡；3D 重建產生的程式碼也在這裡的沙箱執行（ADR 027）。
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# 和 deploy/docker-compose.yml、.env.example 同一組帳密（只綁本機，Windows 經 WSL 的 localhost 轉送連進來）
DB_USER=artrag
DB_PASSWORD=artrag
DB_NAME=artrag

apt-get update
# libgl1 等是 cadquery-ocp 載入時要的（和 CI 同一組）
apt-get install -y --no-install-recommends \
  postgresql postgresql-16-pgvector redis-server \
  python3-venv python3-pip ca-certificates \
  libgl1 libglu1-mesa libxrender1 fonts-dejavu-core

# 3D 重建的執行環境：和 5070 Ti 主機同一組版本（docs/5070ti-host.md「第一次安裝」第 7 步）
python3 -m venv /opt/artrag-cad
/opt/artrag-cad/bin/pip install --no-cache-dir --upgrade pip
/opt/artrag-cad/bin/pip install --no-cache-dir \
  cadquery==2.8.0 cadquery-ocp==7.9.3.1.1 numpy pillow opencv-python-headless
/opt/artrag-cad/bin/python -c "import cadquery; print('cadquery', cadquery.__version__)"

# 一般使用者：wsl.exe --exec（3D 重建的沙箱）預設用它，不用 root 跑模型產生的程式碼
id -u artrag >/dev/null 2>&1 || useradd -m -s /bin/bash artrag
# systemd 關掉：服務由 artrag-services 在前景帶起來，Windows 那邊停掉它就全部停
cat > /etc/wsl.conf <<'EOF'
[user]
default=artrag

[boot]
systemd=false
EOF

# 資料庫：帳號設成 superuser，和 Docker 的 POSTGRES_USER 一樣（建 pgvector extension 要用）
PG_VERSION="$(ls /etc/postgresql)"
pg_ctlcluster "$PG_VERSION" main start
su postgres -c "psql -v ON_ERROR_STOP=1 -c \"CREATE ROLE $DB_USER LOGIN SUPERUSER PASSWORD '$DB_PASSWORD'\""
su postgres -c "createdb -O $DB_USER $DB_NAME"
su postgres -c "psql -v ON_ERROR_STOP=1 -d $DB_NAME -c 'CREATE EXTENSION IF NOT EXISTS vector'"
pg_ctlcluster "$PG_VERSION" main stop

# Redis：和 docker-compose 一樣不存檔（工作狀態以 PostgreSQL 為準，Redis 只傳遞要執行哪個工作）
cat > /etc/redis/artrag.conf <<'EOF'
bind 127.0.0.1
port 6379
protected-mode yes
daemonize no
save ""
appendonly no
maxmemory 128mb
EOF

# Windows 的 start-services.ps1 以 root 執行它：PostgreSQL 在背景、Redis 在前景。
# 這個 wsl.exe 行程活著，WSL 就不會把環境關掉；Windows 那邊停掉它就全部停
cat > /usr/local/bin/artrag-services <<EOF
#!/bin/sh
set -e
pg_ctlcluster $PG_VERSION main start 2>/dev/null || pg_ctlcluster $PG_VERSION main status >/dev/null
exec redis-server /etc/redis/artrag.conf
EOF
chmod 755 /usr/local/bin/artrag-services

apt-get clean
rm -rf /var/lib/apt/lists/* /root/.cache
echo "WSL 環境完成：PostgreSQL $PG_VERSION＋pgvector、Redis、CadQuery（/opt/artrag-cad）"
