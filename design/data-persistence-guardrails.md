# 数据"发版后消失 / 显示空白 / 账号间串扰"根因分析与杜绝清单

> 版本：v20260907132（前端加载层加固）+ v20260907133（账户隔离 + 游客清理周期改为每周日20:00）
> 适用：milktea-trader 所有"发版后数据不见 / 列表空白 / 账号间数据串扰"类问题

---

## 一、结论（先说结论）

经过线上 API 直接核查，本次"可投池不显示 / 股票标签又没了 / 账户管理访客记录为空"三类问题，
**后端数据库里的数据全部都在**：

| 数据 | 线上 API 实测 | 结论 |
|------|--------------|------|
| 可投池 | `/api/pool` total=30，且条目带 `tag_ids`/`tags` | 数据在，是前端没显示出来 |
| 股票标签 | `/api/pool/tags` 返回 4 个标签；pool 条目带 `tags:[{name:"w"}]` | 标签定义+关联都在 |
| 访客记录 | `/api/auth/access-logs` total=2（admin + 1 个 guest 访客） | 数据在，是前端没显示出来 |

**真正的根因只有一条：前端数据加载层"静默失败"。**
`API()` 封装一旦遇到网络抖动 / 网关瞬断 / 限流(429) / 冷启动超时，会直接抛错或被 `catch` 清成空数组，
界面因此一片空白，看起来就像"数据丢了 / 保存没生效"。但库里其实一直有数据。

---

## 二、为什么之前反复"发版后就有问题"

1. **`API()` 没有超时、没有重试、失败不抛明确错误** → 一次瞬时错误就卡死，且没有任何提示。
2. **所有 `loadX()` 的 `catch` 把数据清空却不报错**：
   - `loadPool` 无 try/catch（历史已发现，本次补上）
   - `fetchAccessLogs` 的 `catch` 直接 `accessLogs=[]; accessTotal=0`（看起来就是"空的"）
   - `loadPoolTags` 无保护
3. **用户把"前端没显示"误判为"数据没保存"** → 反复重加数据、反复报"数据保存出问题"。
   实际上保存逻辑（后端 PUT 标签、POST 加池）是正确的，不会误清关联。

---

## 三、本次已做的根治（v20260907132）

1. **`API()` 加固**：15s 超时（AbortController）+ 自动重试（网络/5xx/429 最多 3 次，退避 400ms·n）+ 错误规范化（永远 throw 明确 Error，不再返回 undefined）。
2. **三类数据视图加"可见错误条 + 重试按钮"**：可投池、标签、访问记录加载失败时，顶部红色提示"XX 加载失败：原因，点击重试"，**绝不再静默空白**。
3. **保存入口加错误提示**：`doAddPool`、`addRecToPool` 失败会 `showAlert`，不会"点了没反应"。
4. **三份前端副本（`frontend/`、`frontend-standalone/`、`publish/frontend/`）已同步一致**（md5 校验相同）。

> 注意：本机 `publish/backend/data/app.db` 与 `data/app.db` 等是**过时/测试快照**，不是线上库，
> 不要拿它们判断"线上数据丢没丢"——以线上 API 实测为准。

---

## 四、经验总结（核心原则，以后改版必须遵守）

1. **禁止静默失败**：任何 `API()` 调用 / `loadX()` 必须有 try/catch，失败要**可见 + 可重试**，
   绝不允许 `catch` 里只写 `xxx=[]` 而不提示。这是本次问题的唯一根因。
2. **判断"数据丢没丢"只看后端 API，不看前端空白**：前端空白 90% 是加载失败，不是真丢。
   改版后第一动作是 `curl` 线上 API 核实，再下结论。
3. **多副本必须同步**：前端有 3 份、后端 `publish/` 独立一份。每次改版后，**三份前端同步**、
   `backend/app` → `publish/backend/app` 同步，并 md5 校验，避免"本地改了、线上还是旧版"。
4. **数据库不在代码目录里随意替换**：SQLite 在 `publish/backend/data/app.db`，由云端持久化，
   **不要**把本地空库/测试库 `cp` 上去覆盖线上库。发版只动代码，不动线上 `.db`。
5. **改版后必须做冒烟**：见下方清单，自动核对核心数据非空，防止"发版后才发现空白"。
6. **保存与显示分离**：保存失败要提示；显示失败也要提示。两者都修，用户才不会再误判"保存出问题"。

---

## 五、发版前 / 发版后自检清单（杜绝复发）

### 发版前（本地）
- [ ] 前端三副本 `frontend/index.html` / `frontend-standalone/index.html` / `publish/frontend/index.html` 已同步（md5 相同）
- [ ] 后端 `backend/app/**` 已同步到 `publish/backend/app/**`
- [ ] 改动的 JS 通过语法检查（`node --check` 抽取 `<script>`）
- [ ] 未提交任何密码/密钥明文到 commit message 或代码
- [ ] **未**把本地 `publish/backend/data/*.db` 当作"干净库"覆盖到任何地方

### 发版后（线上，用 `tools/deploy_smoke.py` 或手测）
- [ ] `POST /api/auth/login` 能拿到 token（admin/baofu123）
- [ ] `GET /api/pool?page_size=1` 返回 `total > 0`（本次 30）
- [ ] `GET /api/pool/tags` 返回标签数组（含历史标签，数量不应归零）
- [ ] `GET /api/auth/access-logs?page_size=5` 返回 `total >= 1`
- [ ] 前端页面：可投池 / 标签下拉 / 账户管理→访问记录 三处均能正常渲染；
      若某处空白，应出现**红色错误条 + 重试按钮**，而非纯空白

> 任何一项为 0 / 报错，立即回滚或排查，不要在"看起来空"的状态下交付。

---

## 六、配套工具

- `tools/deploy_smoke.py`：登录后自动核对上述 4 个接口，输出 PASS/FAIL 与计数，
  用于每次发版后的"杜绝空白"自检。用法：`python tools/deploy_smoke.py`
  （默认打线上链接；可用环境变量 `MT_BASE_URL` / `MT_USER` / `MT_PASS` 覆盖）。

---

## 七、账户隔离根因与杜绝（v20260907133）

### 7.1 现象
- admin 登录能看到个股，切换到游客账号却能看到 admin 权限的个股；反过来也成立。
- 可投池、规则、标签、成本持仓备注等应在账号间严格隔离，却出现串号。

### 7.2 根因（两类）
1. **前端切换账号状态未清空**：旧的 `doLogin/doRegister/doGuest/logout` 只 `setItem('tk', newToken)` + `this.me=newUser` + `this.loadDash()`，
   不清除内存里旧账号的 `pool/screens/...` 数据，也不重载应用。切换瞬间若新 token 加载稍慢或失败，页面仍停留在旧账号数据上，
   表现为"能看到对方数据"。
2. **关联表 `tracked_pool_tags` 缺 `user_id` 列**：标签多对多关联表原来只有 `pool_id/tag_id`，没有归属用户。
   删除标签 / 清理游客时无法按 `user_id` 精确清理，容易误删或留孤儿。

### 7.3 本次已做的根治（v20260907133）
1. **前端切换账号彻底重载**：`doLogin/doRegister/doGuest` 先 `localStorage.removeItem('tk')` 再写入新 token，并 `location.reload()`
   让整个应用用新 token 重新初始化（杜绝旧状态残留）。`logout` 同样清空 tk 并 reload。
2. **`tracked_pool_tags` 加 `user_id` 列 + 迁移回填**：`db.py` 迁移给旧关联按 `pool_id→tracked_pool.user_id` 回填 `user_id`；
   新建/解绑关联均写入 `user_id`。
3. **所有清理逻辑按 `user_id` 精确清理**：`_cleanup_guest_data`（游客）、删用户逻辑，均先按 `user_id` 删 `tracked_pool_tags`，
   再用 `pool_id` 子查询兜底孤儿关联，确保不串、不留孤儿。
4. **游客清理周期：每日登录时清理 → scheduler 每周日 20:00 统一清理**（`scheduler.py` 新增 `weekly_guest_cleanup` cron 任务，`misfire_grace_time=3600`）。
   `guest_login` 不再在每次登录时触发清理，避免"切换账号瞬间把共享区清掉导致看起来像丢数据"。
5. **游客提示文案**更新为"每周日 20:00 清理"。

### 7.4 经验（以后改版必须遵守）
1. **切换账号必须整页 reload**：任何登录/登出/切换逻辑，必须 `removeItem('tk')` + `setItem` + `location.reload()`，
   禁止"只换 token 不换内存数据"的写法——这是账号间串数据的最常见成因。
2. **凡是多对多/关联表，必须带 `user_id`**：隔离边界只能靠 `user_id` 过滤，没有 `user_id` 的关联表迟早会串。
3. **清理用 `user_id`，不用"全表 DELETE"**：删除某用户/游客数据时，务必 `WHERE user_id=:uid`，并兜底孤儿关联。
4. **游客清理不要绑在登录动作上**：登录是高频且并发的动作，绑在登录上易在切换瞬间误清；改为独立调度任务（如每周日20:00）。

### 7.5 隔离自检（发版后 / 回归测试）
- 隔离回归思路见 `backend/_test_iso_e2e.py`：用 TestClient 注册两普通用户 + guest，各自建可投池/标签并绑定，
  断言 A 看不到 B、B 看不到 A、游客看不到 admin、标签隔离、关联带正确 `user_id`；再调 `cleanup_guest_periodic()`
  断言 guest 清空、admin 保留、`tracked_pool_tags` 无孤儿。该脚本已实测输出 `ISOLATION_E2E_PASSED`。
- 前端手测：用 admin 登录建一条带标签的可投池；退出→游客登录，确认看不到 admin 的那条；反之亦然。

> 关键提醒：**用户切换账号看到对方数据，90% 是前端旧状态未清空（切换瞬间假象），不是后端真串。**
> 改版后第一动作是用两个不同账号的 token 分别 `curl /api/pool` 核实后端隔离，再下结论。
