# 部署到 Render（免费 · 公网可访问）

本应用是「Python 后端 + Vue 前端」全栈项目，后端负责：账号、数据库、选股/做T分析引擎、
实时行情抓取（腾讯/东财/新浪免费源）。一键静态部署工具（CloudStudio）只能跑纯前端、跑不了后端，
因此用 Render 托管后端（免费套餐即可），前端由后端在同一地址一并托管，最终得到一个公网 URL。

> 已完成的准备工作（本仓库已包含，无需再改）：
> - `Dockerfile`：容器化构建，已绑定 `$PORT`（Render 注入的端口）。
> - `render.yaml`：Render 一键配置（免费套餐、自动生成密钥、健康检查）。
> - 后端已在 `main.py` 挂载 `frontend/`，单一域名同时提供 API 与页面。
> - 启动只依赖内置演示股票池（40 只），不联网即可启动；行情走 HTTP 直连。

---

## 步骤一：把代码放到 GitHub（Render 只认 GitHub/GitLab/Bitbucket）

Render 的 Web 服务不直接拉 Gitee，需先镜像到 GitHub。

1. 打开 https://github.com → 右上角 **+ → New repository** → 取个名（如 `milktea-trader`）→ 选 **Public** → 点 **Create repository**。
2. 在本机项目目录执行（把 `<你的github用户名>` 换成实际值）：
   ```bash
   git remote add github https://github.com/<你的github用户名>/milktea-trader.git
   git push github master
   ```
   > 不会敲命令？把 GitHub 的 **Personal Access Token** 发我，我直接帮你推上去。

## 步骤二：在 Render 一键部署

1. 打开 https://render.com → 用 GitHub 登录（免费注册，无需信用卡）。
2. 控制台 **New + → Web Service** → 选你刚推送的 **milktea-trader** 仓库 → **Connect**。
3. Render 会自动读取 `render.yaml`，确认：
   - Runtime: Docker
   - Plan: Free
   - 其余保持默认
4. 点 **Create Web Service**。首次构建约 2–5 分钟（需装 pandas/akshare 等）。
5. 构建完成后，Render 给出形如 `https://milktea-trader.onrender.com` 的公网地址。

## 步骤三：首次使用

- 打开上面的地址 → 用默认管理员登录：**admin / admin123**（首次会强制改密）。
- 「数据迁移」里可「导出」当前本地数据、「导入」到云端，方便把本地自选池搬过来。
- 行情：默认走腾讯/东财/新浪免费源；点「行情 → 一键导入全A」可扩充股票池（依赖 akshare，若被网络拦截会自动降级演示数据）。

---

## 免费套餐的重要限制（必读）

- **服务空闲 15 分钟后自动休眠，下次访问约 1 分钟冷启动。**
- **免费套餐没有持久盘**：SQLite 数据（账号/自选池/规则/分析）在每次重启或重新部署后会清空。
  - 缓解：定期用「数据迁移 → 导出」备份，重启后「导入」。
  - 想永久保存：在 Render 控制台给该服务**挂载磁盘**（mountPath 填 `/app/backend/data`），
    并取消 `render.yaml` 里 `disks:` 段的注释后重新部署（需升级到 Starter 套餐，约 $7/月）。
- 也可改投 **Fly.io**（免费额度含 3GB 持久盘，适合长期零成本保存 SQLite），需要我换成 Fly.io 部署配置再说一声。

## 常见问题

- **构建失败/超时被墙**：requirements 含 akshare，国内网络拉取慢。可在 Render 的 Build 命令加国内镜像：
  `pip install -r backend/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`
- **页面能开但登录报错 401**：清掉浏览器 localStorage 的 `tk` 再刷新；或确认后端已正常启动（看 Render 日志）。
- **想换域名/开 HTTPS**：Render 免费自带 `*.onrender.com` 的 HTTPS，自定义域名在控制台绑定即可。
