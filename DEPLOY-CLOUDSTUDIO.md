# Cloud Studio 部署指南（国内·免费·可直连）

> 适用场景：想让别人在国内网络也能打开并使用完整功能（实时行情 + 自选池持久化），但不想买服务器。
> 代码已同时托管在 **Gitee**（国内快）和 GitHub。在 Cloud Studio 里**从 Gitee 克隆**最稳。

## 步骤

### 1. 创建工作空间
1. 打开 👉 https://cloudstudio.net （用微信/腾讯云账号登录）
2. 点 **新建工作空间**
3. 来源选 **「代码仓库 / Git 导入」**（或先建空白 Python 工作空间）
4. 仓库地址填：`https://gitee.com/emwyl/milktea-trader.git`
5. 模板选 **Python**（或 Ubuntu + 手动装 Python 也行）
6. 规格选免费档，确认创建

### 2. 启动后端
工作空间打开后，进入**终端（Terminal）**，依次执行：

```bash
# 若是用 Git 导入的，代码已在仓库目录；否则先克隆：
# git clone https://gitee.com/emwyl/milktea-trader.git && cd milktea-trader

cd milktea-trader
bash start.sh
```

- 首次会自动建 `venv`、装依赖（腾讯云镜像，约 1~3 分钟）、随机生成登录密钥、后台启动 uvicorn
- 看到 `>>> 已启动 PID=...` 即成功
- 查看日志：`tail -f backend/server.log`
- 停止服务：`kill $(cat backend/server.pid)`

### 3. 开放公网访问
1. 在 Cloud Studio 界面找 **「访问 / 预览 / 端口」** 按钮
2. 添加转发端口 **8003**（后端监听的端口）
3. 复制生成的公网链接（形如 `https://xxxx-8003.cloudstudio.app` 或类似），发给别人即可打开
4. 用 **`admin` / `admin123`** 登录

### 4. 注册 / 给别人用
- 别人打开链接后，点 **注册** 填用户名密码，即可拥有独立账户、数据互不串（按 `user_id` 隔离）
- 你用 `admin / admin123` 登录，可在设置里管理/禁用其他账号

## ⚠️ 免费档限制（必读）
- **工作空间闲置会被回收/休眠**：休眠后后端停了，别人打不开，需要你重新进工作空间跑 `bash start.sh`
- **工作空间重置会清掉 SQLite 数据**（账号、自选池、规则）：
  - 缓解：定期在「设置 → 数据迁移 → 导出」下载备份 JSON
  - 重置后重新 `bash start.sh`，再「导入」恢复
- 想**稳定常驻 + 数据不丢**，建议升级到付费工作空间或改用腾讯云轻量应用服务器（`docker compose up -d`）

## 本地改完如何同步到 Cloud Studio
- 本地改完 → `git commit` → `git push origin master`（推 Gitee）
- Cloud Studio 终端里 `git pull` 拉最新，再 `bash start.sh` 重启即可

## 排错
- **端口打不开**：确认 start.sh 已成功（看 server.log），并在 Cloud Studio 里正确添加了 8003 端口转发
- **依赖装不动**：脚本已默认走腾讯云镜像；若仍慢，手动 `pip install -r backend/requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple/`
- **8002 端口旧提示**：访问时认准 **8003** 端口，旧 8002 是历史端口，会提示切换到 8003
