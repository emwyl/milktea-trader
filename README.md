# 加油赚奶茶钱 · 散户智能看盘系统（第一期）

个人学习用 A 股看盘/分析网站。把「专业经验 + 系统规则 + 个人偏好」结合，产出交易模型与操作推荐，帮小散户**先控风险、再谈收益**。

> 数据本地优先，隐私不出本机；仅对外拉公开行情，以及（你自行配置的）AI / 通知服务。

## 功能
- 账号密码登录（默认 admin/admin123，**首次登录强制改密**）
- 行情数据层：akshare 真实 A 股日线优先；不可用时降级演示数据（保证可运行）
- 选股模型：投资偏好问卷 → 评分画像 → 关注指标采集 →（配置 DeepSeek Key 后）LLM 建议 → 匹配方案类型；筛选条件全参数化（股价/振幅/箱体/行业联动）
- 短线可投池 + 择时信号系统：自定义加减仓规律（多指标条件 AND/OR + 优先级 + 共振置信度）→ 信号与可读建议
- 风控软引导：信号强制带风险等级 + 风控前置（止损/仓位），强预警但由你决策
- 分析输出与跟踪中心：**看板总览 + 分析中心**双视图（风险预警置顶）
- 通知：console 默认；Server酱/推送加接口预留；每个交易日 15:30 定时复盘
- 模块化解耦：选股/信号/风控/通知/AI 各自独立成库，接口引用、可插拔、参数化客制化

## 运行
1. 虚拟环境（已在 D 盘建好）：`D:\腾讯小龙虾\milktea-trader\venv`
2. 安装依赖（若需重装）：`venv\Scripts\python.exe -m pip install -r backend\requirements.txt`
3. 启动：双击 `start.bat`（或 `bash start.sh`）
4. 浏览器打开 `http://localhost:8000`

## 目录
```
milktea-trader/
├── backend/      FastAPI 后端（app/ 模块化解耦）
├── frontend/     Vue3 单页前端（index.html + 本地 vue.global.prod.js）
├── venv/         D 盘 Python 虚拟环境
└── start.bat/.sh 一键启动
```

## 安全与隐私
- 所有行情、持仓、规则、账号**只存本地 SQLite**（backend/data/app.db），不上传任何未授权平台
- AI Key / 通知 token 本地加密存储，仅向你自己的服务调用
- 仅两类对外网络请求：① 拉取公开行情 ②（你配置后）调用大模型/通知

## 分期
- 第一期（本版）：脚手架 + 登录 + 真实行情层 + 选股模型 + 可投池与择时信号系统 + 看板/分析中心 + 通知骨架
- 第二期：实时行情接入 + 微信定时复盘/即时提醒
- 第三期：AI 对话式偏好采集、个性化推荐、复盘解读
- 后期：迁云 / APP·小程序

## 云服务器部署(发给别人打开)

> ⚠️ **先想清楚再部署**:部署到云服务器 = 你的登录账号/持仓/规则/偏好数据存在云端服务器(不再是"数据不出本机")。给别人打开网站 = 公开访问,请务必先改默认密码、套 HTTPS。介意就把数据留在本地,只把部署当演示用(清空 data/app.db 再传)。

### 方式一:Docker(推荐,一条命令)
前提:服务器装好 Docker + Docker Compose。
```bash
# 1. 把整个项目(含 Dockerfile/docker-compose.yml/backend/frontend)上传到服务器
# 2. 在项目根目录执行:
docker compose up -d --build
# 3. 打开 http://服务器公网IP:8000
#    - 阿里云/腾讯云等:安全组放行 8000 端口;宝塔等面板:防火墙放行
#    - 数据持久化在宿主机 ./data,容器重建不丢
```

### 方式二:裸机 systemd(无 Docker)
```bash
# 服务器: Ubuntu/Debian,Python ≥3.10
sudo apt install -y python3-venv nginx
cd /srv/milktea-trader
python3 -m venv venv
venv/bin/pip install -r backend/requirements.txt

# systemd 服务 /etc/systemd/system/milktea.service:
#   [Unit] Description=milktea-trader
#   [Service] WorkingDirectory=/srv/milktea-trader/backend
#   ExecStart=/srv/milktea-trader/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
#   Restart=always
#   [Install] WantedBy=multi-user.target
sudo systemctl daemon-reload && sudo systemctl enable --now milktea
```

### 方式三:nginx 反代 + HTTPS(生产推荐)
在方式二基础上,用 nginx 把 80/443 转到 8000,并配置 SSL 证书:
```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;
    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### 部署后必做(安全)
1. **改默认密码**:登录 admin/admin123 → 设置页改密码(数据库标记了 must_change_pw,首次登录应强制改)
2. **HTTPS**:域名 + 证书,别裸奔 HTTP
3. **防火墙**:只放行 80/443(或 8000);不要对公网开 SQLite/后台端口
4. **数据卷备份**:定期备份服务器 ./data/app.db(你的全部数据都在里面)

### 一键脚本(推荐,含全部步骤)
服务器上执行(把项目上传后):
```bash
chmod +x deploy_nginx.sh
sudo ./deploy_nginx.sh   # 先改脚本顶部 DOMAIN= 为你的域名
```
脚本自动完成:装 nginx/certbot → 建 venv 装依赖 → systemd 服务 → nginx 80→443 反代 → certbot 免费证书。失败兜底为自签证书(浏览器会提示不安全,域名解析好后重跑 certbot 即可)。
