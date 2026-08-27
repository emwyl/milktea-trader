#!/usr/bin/env bash
# ============================================================
#  加油赚奶茶钱 · 云服务器一键部署脚本(nginx + HTTPS 生产模式)
#  适用: Ubuntu/Debian 云服务器(阿里云/腾讯云/AWS 等),已备案或可解析域名
#
#  用法(在服务器上):
#     chmod +x deploy_nginx.sh
#     sudo ./deploy_nginx.sh
#
#  脚本会:
#    1. 安装 python3-venv / nginx / certbot
#    2. 拉取项目(把项目放到 /srv/milktea-trader 或从 git 克隆)
#    3. 建 venv + 装依赖
#    4. 注册 systemd 服务(uvicorn 监听 127.0.0.1:8000)
#    5. 生成 nginx 配置(80→443 跳转 + 443 反代)
#    6. certbot 自动签发 HTTPS 证书(免费 90 天,自动续期)
# ============================================================
set -e

# ============ 按你的实际情况改这里 ============
DOMAIN="your-domain.com"                    # 你的域名(已解析到本服务器)
PROJECT_DIR="/srv/milktea-trader"            # 项目存放路径(脚本会自动创建并从当前目录拷贝/或你预先放好)
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"     # 本脚本所在目录(即项目根目录)
PORT=8000
# ===============================================

echo "==== [1/7] 安装系统依赖 ===="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx

echo "==== [2/7] 放置项目 ===="
mkdir -p "$PROJECT_DIR"
if [ "$SRC_DIR" != "$PROJECT_DIR" ]; then
  cp -r "$SRC_DIR/backend" "$SRC_DIR/frontend" "$SRC_DIR/README.md" "$PROJECT_DIR/"
fi
cd "$PROJECT_DIR"

echo "==== [3/7] Python 虚拟环境 + 依赖 ===="
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r backend/requirements.txt

echo "==== [4/7] systemd 服务 ===="
cat > /etc/systemd/system/milktea.service <<EOF
[Unit]
Description=MilkTea Trader (加油赚奶茶钱)
After=network.target

[Service]
WorkingDirectory=$PROJECT_DIR/backend
ExecStart=$PROJECT_DIR/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=3
Environment=TZ=Asia/Shanghai

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now milktea
sleep 2
echo "服务状态: $(systemctl is-active milktea)"

echo "==== [5/7] nginx 配置 ===="
cat > /etc/nginx/sites-available/milktea <<EOF
# 80: 全部跳转 HTTPS
server {
    listen 80;
    server_name $DOMAIN;
    return 301 https://\$host\$request_uri;
}

# 443: 反代到 uvicorn
server {
    listen 443 ssl;
    server_name $DOMAIN;

    # 证书由第 6 步 certbot 填入,首次先用自签占位
    ssl_certificate     /etc/nginx/ssl/milktea-selfsigned.crt;
    ssl_certificate_key /etc/nginx/ssl/milktea-selfsigned.key;

    # 禁止 iframe 外嵌(防被别人套壳)
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

echo "==== [6/7] 先发一个自签证书占位(避免 nginx 校验失败) ===="
mkdir -p /etc/nginx/ssl
openssl req -x509 -nodes -days 90 -newkey rsa:2048 \
  -keyout /etc/nginx/ssl/milktea-selfsigned.key \
  -out /etc/nginx/ssl/milktea-selfsigned.crt \
  -subj "/CN=$DOMAIN" >/dev/null 2>&1

ln -sf /etc/nginx/sites-available/milktea /etc/nginx/sites-enabled/milktea
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "==== [7/7] certbot 签发正式证书(需域名已解析到本机) ===="
if [ "$DOMAIN" != "your-domain.com" ]; then
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m admin@"$DOMAIN" --redirect || \
  echo "!!! certbot 失败:可能域名未解析到本机,或 80 端口未放行。"
  echo "    失败也能用(自签证书),但浏览器会提示不安全;域名解析好后重跑:"
  echo "    sudo certbot --nginx -d $DOMAIN"
else
  echo "!!! 未设置域名(仍为 your-domain.com)。改脚本顶部 DOMAIN= 后再跑,或用自签证书直接访问。"
fi

echo ""
echo "================ 部署完成 ================"
echo " 网站: https://$DOMAIN"
echo " 服务: systemctl status milktea"
echo " 数据: $PROJECT_DIR/backend/data/app.db (记得定期备份)"
echo " 提醒: 首次登录请改默认密码 admin/admin123"
echo " 提醒: 阿里云/腾讯云安全组需放行 80/443 端口"
echo "========================================="
