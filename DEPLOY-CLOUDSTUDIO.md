# Cloud Studio 部署指南（国内·免费·可直连）

> 适用场景：想让别人在国内网络也能打开并使用完整功能（实时行情 + 自选池持久化），但不想买服务器。
> 代码已同时托管在 **Gitee**（国内快）和 GitHub。在 Cloud Studio 里**从 Gitee 克隆**最稳。
> 
> 注意：Cloud Studio 的「端口插件」只能自动识别**前台运行**的进程，因此本指南使用 `start-cloudstudio.sh`（前台启动），不要用 `start.sh`（后台 nohup，端口插件识别不到）。

## 步骤

### 1. 创建工作空间
1. 打开 👉 https://cloudstudio.net （用微信/腾讯云账号登录）
2. 点 **新建工作空间**
3. 来源选 **「代码仓库 / Git 导入」**（或先建空白 Python 工作空间）
4. 仓库地址填：`https://gitee.com/emwyl/milktea-trader.git`
5. 模板选 **Python**（或 Ubuntu + 手动装 Python 也行）
6. 规格选免费档，确认创建

### 2. 启动后端
工作空间打开后，进入**终端（Terminal）**，先确认代码位置：

```bash
ls /workspace
```

- 如果直接看到 `backend/`、`frontend/`、`start-cloudstudio.sh` 等文件，说明项目在 `/workspace` 根目录，直接执行下面命令。
- 如果看到的是 `milktea-trader/` 子目录，则先执行 `cd milktea-trader` 再执行下面命令。

然后启动（使用**前台模式**，Cloud Studio 端口插件才能自动识别）：

```bash
cd /workspace
bash start-cloudstudio.sh
```

- 首次会自动建 `venv`、装依赖（腾讯云镜像，约 1~3 分钟）、随机生成登录密钥、前台启动 uvicorn
- 看到 `>>> 启动 milktea-trader on 0.0.0.0:8003` 并持续显示 uvicorn 日志即成功
- 停止服务：按 `Ctrl+C`，或在另一个终端执行 `kill $(cat backend/server.pid)`

### 3. 开放公网访问
1. 启动 `start-cloudstudio.sh` 后，稍等几秒钟，Cloud Studio 左侧「端口管理」面板会自动识别出 **8003** 端口卡片
2. 在 8003 端口卡片上点击 **「查看预览」** 或链接图标，复制生成的公网链接（形如 `https://xxxx-8003.cloudstudio.app`）
3. 把链接发给别人即可打开；用 **`admin` / `admin123`** 登录

> 如果面板没有自动出现 8003，先确认终端里 `uvicorn` 日志已经显示 `Uvicorn running on ... 8003`，再等 5~10 秒刷新面板。仍未出现则截图终端日志发给我。

### 4. 注册 / 给别人用
- 别人打开链接后，点 **注册** 填用户名密码，即可拥有独立账户、数据互不串（按 `user_id` 隔离）
- 你用 `admin / admin123` 登录，可在设置里管理/禁用其他账号

## ⚠️ 免费档限制（必读）
- **工作空间闲置会被回收/休眠**：休眠后后端停了，别人打不开，需要你重新进工作空间跑 `bash start-cloudstudio.sh`
- **工作空间重置会清掉 SQLite 数据**（账号、自选池、规则）：
  - 缓解：定期在「设置 → 数据迁移 → 导出」下载备份 JSON
  - 重置后重新 `bash start-cloudstudio.sh`，再「导入」恢复
- 想**稳定常驻 + 数据不丢**，建议升级到付费工作空间或改用腾讯云轻量应用服务器（`docker compose up -d`）

## 本地改完如何同步到 Cloud Studio
- 本地改完 → `git commit` → `git push origin master`（推 Gitee）
- Cloud Studio 终端里 `git pull` 拉最新，再 `bash start-cloudstudio.sh` 重启即可

## 排错
- **端口打不开**：确认 `start-cloudstudio.sh` 已在终端前台运行（能看到 uvicorn 日志滚动），Cloud Studio「端口管理」面板会自动出现 8003 卡片；若未出现，先等 10 秒刷新，再截图终端日志发给我
- **依赖装不动**：脚本已默认走腾讯云镜像；若仍慢，手动 `pip install -r backend/requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple/`
- **8002 端口旧提示**：访问时认准 **8003** 端口，旧 8002 是历史端口，会提示切换到 8003
- **bad interpreter: /usr/bin/env^M**：脚本换行符问题；已加 `.gitattributes` 保证从 Gitee 拉下的是 LF，若仍遇到，在终端执行 `sed -i 's/\r$//' start-cloudstudio.sh` 修复
