---
name: cicd-deploy-monitor
description: 在多仓微服务（BrowserGateway / GlobalInstanceDeliverService / MediaCacheService）项目中，端到端完成"需求实现 → 本地测试 → Git 提交推送 → 拉取并解压 CI/CD 日志包 → AI 解读日志判断编译/运行/测试是否通过 → 必要时修复代码并重提交"的循环。实现阶段通过派 `general` 子代理完成。使用场景：用户提出一个业务需求，且希望自动驱动公司内部 CI/CD 流水线并基于真实运行/评测日志决定是否需要再修复。Use ONLY when the user is in the sbg monorepo and wants a requirement to flow through the internal CI/CD pipeline with pipeline log verification.
---

# CI/CD 部署监控 Skill（sbg 多仓项目）

本 skill 驱动一个完整的"开发 → 推送 → 流水线 → 拉日志 → AI 判定 → 修复或通过"闭环。

## 项目结构与仓库缩写

工作目录：`/home/lele/project/work/csp-ysj/sbg`

| 仓库目录 | 仓库名 | 缩写 | 远程 (ssh) |
|---|---|---|---|
| `BrowserGateway/` | BrowserGateway | `bgw` | `git@github.com:Cogather/BrowserGateway.git` |
| `GlobalInstanceDeliverService/` | GlobalInstanceDeliverService | `gids` | `git@github.com:Cogather/GlobalInstanceDeliverService.git` |
| `MediaCacheService/` | MediaCacheService | `mc` | `git@github.com:Cogather/MediaCacheService.git` |

> **缩写是流水线判定本次产物归属的 key**：后台日志 zip 文件名格式 `{name}_{commit_id}_{datetime}.zip`，其中 `{name}` 必须用 `bgw` / `gids` / `mc` 之一。
>
> **`commit_id` 段实际就是 git 短 hash**（流水线这边只截前 8 位左右，不放完整 40/64 位 SHA）。例：`mc_a586ff5e_20260609202544.zip` 对应 `mc` 仓某次提交的短 hash `a586ff5e`。所以阶段 2 拿到 `git rev-parse --short=8 HEAD` 即可，**不要再尝试传完整 SHA** 给查询/下载接口。

## 工作流（必须严格按顺序执行）

### 阶段 0：澄清需求与归属仓库

在动手前**必须**向用户确认：

1. 需求描述（功能/接口/行为）
2. 受影响仓库（一个或多个）以及**每个仓库具体改动的位置**（模块、文件）
3. 验收口径（看哪些日志字段算"通过"）

不要假设。若用户说"实现 xxx"，先反问"具体在哪个仓库/哪个文件？判断成功的日志关键字是什么？"

### 阶段 1：实现 + 本地测试（**通过自代理完成**）

**不要**让主线程自己去读仓库、改代码、跑构建——任务粒度太大，容易把上下文塞爆。**必须**用 `task` 工具派一个 `general` 子代理去做，主线程只负责接收子代理的最终汇报。

调用形式：

```text
task(
  subagent_type: "general",
  description: "实现并本地测试 <需求简述>",
  prompt: <见下方模板>
)
```

子代理 prompt 模板（**每个仓库一份**，互不干扰）：

```text
你是一名在沙箱里工作的 Go/Java 工程师。仓库根：/home/lele/project/work/csp-ysj/sbg/<Repo>。
需求：<从主代理传过来的那段需求描述，原文粘贴>
验收口径：<主代理从用户那里确认的"什么算通过"的口径>

请按以下顺序执行，并把每一步的原始命令输出粘贴到最终汇报里：

1. cd <Repo>，先 cat AGENTS.md / README.md / docs/，了解构建/启动方式。
2. 用 grep/glob 定位与本次需求相关的文件，给出"计划改哪些文件、怎么改"。
3. 改代码。**禁止添加任何注释**（除非用户明确要求）。
4. 本地按该仓库既有方式跑构建 + 主路径测试，至少要看到一次"主路径未坏"的证据。
5. 汇报给我：
   - 改了哪些文件（path:line 列表）
   - 本地测试的最终结论（PASS/FAIL + 关键证据原文 1-3 行）
   - 任何遗留的"应该没问题但我没把握"的点

注意：
- 不要 push；不要 commit；不要碰其它仓库。
- 如果本地测试失败，继续修，直到通过为止；超过 5 轮修不过就停下，把当前状态交还主代理。
```

主线程拿到子代理的"本地通过"汇报后，才进入阶段 2。**子代理没说通过，就别提交。**

### 阶段 2：提交并推送（必须新 commit + push）

**每个受影响仓库**都走完整流程：

```bash
cd /home/lele/project/work/csp-ysj/sbg/<Repo>
git status
git add -A
# 重要：必须新 commit，绝不合并到已有 commit
git commit -m "<简明描述需求> [AI-Generated]"
git rev-parse --short=8 HEAD   # 与流水线 zip 中 commit 段保持一致（短 hash）
git push origin <当前分支>
```

如果 `git push` 因权限/网络失败：**停下来告诉用户**，不要自己重试无谓的次数。

收集本次会话涉及的 `(仓库, 缩写, commit_id)` 列表，下一阶段要用：

```
submitted = [
  ("BrowserGateway", "bgw", "<bgw_commit>"),
  ("MediaCacheService", "mc", "<mc_commit>"),
  ...
]
```

### 阶段 3：启动日志轮询（py 脚本，后台跑）

用 `scripts/log_poller.py`（同目录）轮询日志服务，下载并解压本次提交的产物。

> **bash 工具 timeout 必填**。脚本默认内部 30 分钟超时 + 10 秒轮询，**opencode bash 工具的默认 120s 会直接把它砍掉**。这一条 bash 调用必须显式传一个远大于脚本内超时的 `timeout`（毫秒），**建议 35 分钟 = 2100000ms**。其它 bash 调用维持默认即可，**不要**顺手把别的地方也加大。

**关键参数**（通过环境变量或 CLI 参数传入）：

- `LOG_BASE_URL` 默认 `http://81.70.210.89:8080`
- `LOG_POLL_INTERVAL` 默认 `10` 秒
- `LOG_OUT_DIR` 默认 `./_cicd_logs/`
- `LOG_WATCHES` 用 `name=commit` 形式，逗号分隔；`commit` 是阶段 2 拿到的**短 hash**（如 `a586ff5e`），不是完整 SHA。例：
  `LOG_WATCHES=bgw=<bgw_short>,mc=<mc_short>,gids=<gids_short>`

**轮询逻辑**（脚本已实现，下面是契约）：

1. 启动时对 `submitted` 里每个 `(name, commit)` 调用 `GET /query?name=<name>&commit=<commit>`。
2. 每 10 秒重试；找到文件后 `GET /download?filename=<name>` 存到 `LOG_OUT_DIR/raw/<name>/<filename>.zip`，然后解压到 `LOG_OUT_DIR/extracted/<name>/<commit_id>/`。
3. 同一 `(name, commit, datetime)` 不会重复下载（维护 `state.json`）。
4. 找到**全部** watches 的文件后优雅退出（返回码 0）；超时（默认 30 分钟，可调 `LOG_TIMEOUT_SEC`）则返回码 2。
5. 退出前打印一份**产物清单**（每个仓库有哪些文件、解压到哪）。

**启动方式**：

```bash
# 推荐：前台跑，bash 工具 timeout 调到 35 分钟（2100000ms）以上
python3 .opencode/skill/cicd-deploy-monitor/scripts/log_poller.py \
  --watch bgw=<bgw_short> --watch mc=<mc_short> \
  --out ./_cicd_logs/ --base-url http://81.70.210.89:8080
```

或显式环境变量：

```bash
LOG_WATCHES="bgw=<bgw_short>,mc=<mc_short>" \
LOG_OUT_DIR=./_cicd_logs \
LOG_BASE_URL=http://81.70.210.89:8080 \
python3 .opencode/skill/cicd-deploy-monitor/scripts/log_poller.py
```

### 阶段 4：AI 解读日志，决定是否修复

解压目录里至少包含这几类日志（具体哪些由 CI 决定；脚本只是解 zip，**不预判结构**）：

- `pipeline.log` / `build.log` / `compile.log` —— 编译是否通过
- `service.log` / `runtime.log` / `stdout.log` —— 服务端执行
- `e2e.log` / `test.log` / `evaluate.log` —— 评测/端到端

**判读规则**（按优先级）：

1. **编译失败**（任一仓库 `build.log` 含 `error:` / `BUILD FAILURE` / 退出码非 0）：
   → 必须修复，回到阶段 1。
2. **服务启动失败 / 运行时异常**（`service.log` 含 `Exception` / `panic` / `FATAL` 且与本次需求相关）：
   → 必须修复，回到阶段 1。
3. **评测/测试失败**（`e2e.log` 含 `FAIL` / 失败用例数 > 0，且失败用例与本次需求相关）：
   → 必须修复，回到阶段 1。
4. **完全通过**：
   → 在终端给用户一份"通过"报告（含 commit_id、日志路径、关键 success 关键字），结束本轮。

**修复时的纪律**：

- 每次修复**必须**新 commit（即使只是改一个字符）。commit message 加 `fix:` 前缀。
- 修复后**完整重跑**阶段 1 本地测试，再 push。
- 回到阶段 3 重新轮询**新 commit** 的日志产物。脚本会按 commit 区分目录，不会污染上一轮。
- 设置一个最大重试上限（默认 5 轮）防止死循环。超过上限要停下来问用户。

### 阶段 5：交付报告

最终给用户：

- 每个仓库最终的 commit_id（短 hash + 标题）
- 每个仓库 CI 状态摘要（一行）
- 关键证据：粘贴 1-3 行成功日志原文（编译 OK、测试 PASS、关键接口返回等）
- 失败轮次的简短回顾（修了什么、为什么）

## 脚本与文件

- `scripts/log_poller.py`：日志轮询 + 下载 + 解压
- 生成的产物在 `_cicd_logs/raw/<name>/` 与 `_cicd_logs/extracted/<name>/<commit_id>/`
- 轮询状态在 `_cicd_logs/state.json`（重启后会跳过已下载文件）

## 硬性约束（违反任何一条都算 skill 失败）

1. **绝不在已有 commit 上 amend** 提交本次需求；必须有独立 commit。
2. **绝不在用户没确认仓库归属前**开始改代码。
3. **绝不在本地测试没通过前** push。
4. **绝不在没拉日志 / 没读日志**的情况下宣布"通过"。
5. **绝不在没有新 commit** 的情况下重推同一份代码当作"修复"。
6. **绝不在死循环里改 5 轮以上**而不告诉用户。
7. 不要在代码里加注释（除非用户明确要求）。
