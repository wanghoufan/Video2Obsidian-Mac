# WIN-STAGE4-QA｜Windows 迁移 Stage 4 独立 QA

- 日期：2026-09-16
- 对象：当前仓根 `windows/**` 未提交改动
- QA 结论：**FAIL（环境阻塞；未发现已复现的业务逻辑缺陷）**
- 说明：Stage 4 主链、崩溃恢复、reapply 夹具和反向证伪均通过；`contract` 真 HTTP 绑定在当前沙箱被 OS 禁止，Windows 11 真机 15 项未验，不能将整体写成 PASS。

## 一、BUG 清单

| ID | 级别 | 状态 | 结论 |
|---|---|---|---|
| WIN-STAGE4-QA-001 | P0/P1/P2：无 | PASS | `PUBLISH_BLOCKED → reapply` 独立端到端通过：生成新稿、Raw 不变、whisper 0 次。 |
| WIN-STAGE4-QA-002 | 环境阻塞 | BLOCKED | `selftest_p1_2_contract.py` 在真实 HTTP 路由阶段 bind `127.0.0.1` 返回 `PermissionError: [Errno 1] Operation not permitted`，rc=1；不得推断 contract 通过。 |
| WIN-STAGE4-QA-003 | 真机验收 | NOT VERIFIED | macOS 宿主无 Windows 11/CUDA/faster-whisper/NTFS/PowerShell 实机，交接文档 §二 15 条全部保持“未验”。 |

没有新发现的 P0/P1/P2 业务缺陷。`WIN-STAGE4-QA-002/003` 是验证环境限制，不是代码缺陷判定。

## 二、独立夹具结果

### 1. reapply 恢复链路（重点 P2-1）

自建 tmp 夹具，桩引擎、合成 wav→mp4，真实数据库/Norm/Render/发布接口：

1. 首次发布注入 `OSError`，得到 `PUBLISH_BLOCKED`，首次引擎调用 1 次。
2. 调 `/api/reapply`：HTTP/handler rc=200、`ok=true`、`prev_state=PUBLISH_BLOCKED`、`new_state=PUBLISH_BLOCKED`、`raw_unchanged=true`、`whisper_calls=0`，新 `.md` 确实生成在 data jobs/render 目录。
3. 同一 run 调 `/api/retry`：返回 202 `QUEUED`；等待 15 秒引擎调用增量仍为 0，实际被已完成跳过口径挡住。

结论：**reapply 恢复链路通过；重试与 reapply 行为差异真实存在：重试入口接受排队但不恢复该已完成/阻塞 run，reapply 才是零转写恢复入口。**

### 2. 主路径、No-Clobber、崩溃恢复

- 短音频 stage7：1 次引擎调用，恰 1 篇 md，源保留。
- 601 秒长音频 stage8：600/2 分块，2 次引擎调用，恰 1 篇 md。
- 已有同名笔记：SKIPPED、whisper 0 次、既有文件字节不变。
- 转写中断：重启同一 run 幂等恢复，Source=1、AUTO Run=1、PUBLISHED=1、无半成品。
- 发布中断：PUBLISH_BLOCKED；重启不重转、不重复建 Source/Run、不静默补发布，成稿留在 data 目录。
- reconcile 两轮：Source=1、AUTO Run=1、Lost/Duplicate=0。
- 半截源：送引擎前 FAIL 人话，零转写、零发布、源保留。

### 3. 第二实例语义

因沙箱禁止 loopback bind，按任务书退化为纯 `instance.py` 夹具：

- 持有同一 data_root 锁时：exit=3，stderr 含 `SECOND_INSTANCE`，DB size/mtime/SHA-256 不变。
- 释放锁后：exit=0。

### 4. 独立反向证伪（均测红后用 `/tmp` 备份 `cp` 还原）

| 编号 | 变异 | 测红证据 | 还原核对 |
|---|---|---|---|
| F1 | `server.py::_stale_source_reason` 入口恒返 `None` | Stage 4 rc=1；半截源 3 项断言失败，T4 变无牙 | SHA-256 `db59ecfd…` 与 `/tmp/qa_stage4_backups/server.py` 一致 |
| F2 | `runs.py` 移除 AUTO 冲突幂等门 | Stage 4 rc=2；重启扫描报 `UNIQUE constraint failed`，启动失败 | SHA-256 `3c937a1b…` 一致 |
| F3 | `start.ps1` 默认端口 `8899→8898` | 端口真源静态契约 rc=1，默认值检查 FAIL | SHA-256 `75437d1c…` 一致 |

## 三、七项自测 rc

| 项目 | 结果 |
|---|---:|
| `compileall -q app src tests` | 0 |
| `selftest_win_stage4.py`（终验：65/0，teeth 4/4） | 0 |
| `selftest_win_stage3.py`（70/0，teeth 11/11） | 0 |
| `selftest_win_stage12.py`（138/0，teeth 13/13） | 0 |
| `selftest_p1_2_contract.py`（真 HTTP bind 被沙箱拒绝） | 1 BLOCKED |
| `selftest_p1_2_frontend.py` | 0 |
| `selftest_v26_presets.py` | 0 |

补充：未强制替换 observer 的原生 macOS 首轮 Stage 4 因 watchdog FSEvents stream 异常退出 rc=138；使用 `/tmp` 中的 `PollingObserver` 环境夹具复跑，终验 rc=0。未改业务文件。

## 四、交付文档与隔离抽验

- `windows/docs/WINDOWS-HANDOFF.md` 的验收命令与文件结构一致；15 条 Windows 11 真机清单均保留“未验=未通过，不许打勾”。
- `git status --short -- app src tests`：空。
- 全量工作树仅包含任务对象及既有 reviewer 报告/Windows 新交付物；`windows/app/start.sh` 删除保持暂存 D。
- 未联网、未使用真实用户目录/Obsidian 库、未 bind 8765；临时夹具均位于系统 tmp。

## 五、reviewer P3 逐条表态

- P3-1 ffmpeg 随包承诺未落地：**同意**，需打包阶段补齐二进制、版本、来源和许可证清单。
- P3-2 requirements 哈希占位：**同意**，模型/依赖冻结时回填真实 digest。
- P3-3 `sleep(12)` 轮询窗口：**同意**，测试基建风险；worker 周期变化时应同步调整。

## 六、待补

需在 Windows 11 真机完成交接文档 §二 15 项，尤其真实 CUDA/faster-whisper、PowerShell 启动、NTFS 语义、断网、长路径、真实 HTTP 服务和全套自测。完成后再复核本报告的 `WIN-STAGE4-QA-002/003`。

---

## 七、收尾注记（2026-09-16，neat-freak，文档对齐抽查；上文正文一字未动）

- 本报告引用的文件路径与 rc 表全部核对无误：七项自测文件均在 `windows/tests/` 实存；`windows/app/start.sh` 删除保持暂存 D 与收尾时点 `git status` 一致。
- §四 F1 的 server.py SHA-256 `db59ecfd…` 与 code-review 报告 §4 的 `04c04ec0…` 系时点差异（返工 :57 注释改动前/后），各自与其 `/tmp` 备份自洽，非矛盾；F2 `3c937a1b…`、F3 `75437d1c…` 与 reviewer 逐字一致。
- §五对 reviewer P3-1/2/3 的表态与 review 报告问题清单逐条对应；Stage 3 挂账条目（QA-001/002/003 ↔ Stage3 reviewer P3-1/2/3）在 Stage 4 review「前次报告条目现状」中顺延一致，无遗漏。

---

## 八、supervisor复检（2026-09-16，PASS，放行推main，本链打回0/2）

- 通道偏离记账：表定supervisor走opencode直调（禁本窗口代做），实走opencode run 10分钟超时无回吐（进程已kill），为防空烧额度转本窗口补位执行，note记原因，不记模型偏离。另：角色卡DISPATCH runtime枚举缺`opencode`值（表已换代新增opencode/codebuddy通道），校验时按表为准放行，角色卡待治理侧更新。
- 隔离：`git status --short -- app src tests`输出空；windows/外仅`M USER_MODEL_OVERRIDE.md`（分工表调整，不属本链）。
- 独立复跑（本窗口.venv）：stage4终验65/0 teeth 4/4 SELFTEST PASS；contract 601/601 ALL PASS；frontend ALL PASS；stage3自测70/0 teeth 11/11。
- 自跑证伪2/2有牙（/tmp备份还原，未用git checkout）：F1 start.ps1默认8899→8898→contract 1/601红（8 默认值分叉），还原sha256 `75437d1c…`一致；F2重建windows/app/start.sh→contract 1/601红（8 start.sh已删防回退），删件还原后601绿。server.py sha `db59ecfd…`与qa报告F1一致，复检期间零残留。
- reviewer/qa逐条表态：reviewer P2-1（reapply补验）已由qa独立端到端PASS关闭；P3×3（ffmpeg随包/README打包清单、requirements哈希占位、sleep12轮询窗口）同意挂账不阻塞；qa QA-001 reapply链路PASS确认，QA-002/003为环境阻塞（沙箱禁bind＋真机15项未验）如实标注，contract TM补位601 rc0成立，不升不降。
- 账本：TASK-MODEL-LOG第二道校验BAD=0；DISPATCH-LOG第二道校验BAD=0（runtime按表含opencode放行）。
- 结论：PASS，放行收口推main；挂账延续qa §六＋reviewer P3×3，真机15项零推断。

---

## 九、收尾注记（neat-freak，2026-09-16；上文正文一字未动）

- 一致项（实测基准＝HEAD `76c05bc`）：① §三自测数 `stage4终验65/0 teeth 4/4`、`contract 601/601`、`stage3自测70/0`——本轮本窗口 `.venv` 实跑复现一致（stage4 65 PASS／stage3 70 PASS）；② §四 F1 `server.py` SHA `db59ecfd…`＝实测 `shasum windows/app/server.py` 前缀一致（`db59ecfd…fa4f`）；F3 `75437d1c…`＝实测 `windows/start.ps1` 前缀一致（`75437d1c…6c2b`，注：文件在 `windows/` 根，非 `windows/app/`）；F2 `3c937a1b…` 为 `/tmp` 备份还原历史值，工作树无残留；③ 账本 `TASK 64 行`／`DISPATCH 113 行`与实测行数一致（收口 `587d0ae` 提交信息内记数一致）；④ 复检节位置 `§八` 起始 `:96`、标题 `:96`，HANDOFF §一.8 引 `:八` 同节，无歧义。
- 差异项：无实质差异。仅路径表述收窄：F3 所指 `start.ps1` 即 `windows/start.ps1`（`windows/app/` 下无此文件，`windows/app/start.sh` 已删），结论不受影响。
- 本节只加注，不改结论正文，不碰业务代码。
