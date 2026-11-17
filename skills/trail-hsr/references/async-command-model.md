# Async Command Model

- daemon-backed 业务命令默认使用 daemon 端异步单例 job 模型；同一 daemon 同一时间只允许一个游戏操作运行，`trail start`、`trail ocr read`、`trail input ...`、`trail cw ...` 都可能先返回 `state=running request=<job_id> waited=<n>`。
- `request=<job_id>` 是后台 job id，不是本次 socket call id；它主要用于恢复、排障、取消或跨命令查询。常规续查不要求 Agent 手动填写这个 id。
- 除长战外，常规续查方式是原样重发同一条业务命令；不要把 running 当作失败，也不要换成不同业务命令重新执行。长战按 job id 查询状态及结果。
- `trail start` running 后的常规续查示例：继续运行 `trail start`。
- 长战 running 后使用 `trail daemon request-status --request-id <job_id>`；终态使用 `trail daemon request-result --request-id <job_id>`，不重发同一战斗命令查状态。
- 除长战外，如果再次返回 running，继续原业务命令；如果返回终态业务输出（例如 `status=...`、`shot path=...`），再按该命令所属 skill 的正常输出协议继续。
- `info next_action=<command> request=<job_id>` 是强提示：除长战外，下一步优先照它重发同一业务命令；长战优先按 ID 查询；若同一行带 `session=<id>`，续查命令必须带该 session。通常无需把 `request=<job_id>` 改写成 `--request-id`。
- `DAEMON_BUSY` 表示已有不同游戏操作正在运行；不要排队或另起动作。若活动命令是 `cw.battle.run`，按输出使用 `trail daemon request-status --request-id <job_id>` 查询活动 job，终态再用 request-result；其他命令按 `recover action=<command> request=<job_id> [session=<id>]` 重发对应业务命令。
- `trail daemon request-status --request-id <id>`、`trail daemon request-result --request-id <job_id>` 和 `trail daemon request-cancel --request-id <id>` 是控制面 / 恢复面命令，除长战进度查询外不是常规轮询首选。只有在 stdout 丢失、跨命令恢复、busy/recover 无法重发原命令、unknown result、tainted 或需要取消时使用。
- `trail daemon request-result --request-id <job_id>` 用于已知 job id 或 call id 的终态结果回放；业务命令常规路径仍优先重发原命令。
- `trail daemon request-cancel --request-id <id>` 是 soft cancel。若取消发生在 UI side effect 之后，可能返回 `REQUEST_CANCEL_UNKNOWN` / `tainted=1`；此时停止普通操作，按输出 `recover action=daemon.reconcile_session session=<id>` 或 advanced 恢复链路收敛上下文。
- 没有显式 `--request-id` 时，重发同 payload 只会附着当前仍在运行的 singleton job；如果匹配 job 已终态，普通业务命令不会 replay 旧结果，而是按一次新的业务命令提交。需要查看旧终态结果时，使用 `trail daemon request-status --request-id <job_id>` 或 `trail daemon request-result --request-id <job_id>`。

- 长战推荐 `trail --no-wait cw battle run --session <session_id> --timeout 420`，保存返回的 job id；用 `trail daemon request-status --request-id <id>` 查看最近确认的阶段、节点、HUD 和证据时间。查询不会发游戏输入或额外截图，进度是最近观察值，不一定是当前画面。
- job `final=1` 后用 `trail daemon request-result --request-id <id>` 获取原结果，call id 也可用于恢复。job 完成不等于战斗完成：若业务结果为 `status=in_progress`，先读同帧证据，再以同一 session 新运行 `cw battle run` 续跑。评价、战中 HUD、结算和终局详情是独立来源，应分别解读。

- CLI 断线但 daemon 仍在运行时，通过原 job/call id 查询；若旧 daemon 进程已退出，旧 job 可能仍显示 running，不能据此认为仍在操作游戏，也不要一直等待旧 job。确认旧进程已退出后，使用同一 session 新调用 `cw battle run`，让它根据保存的检查点和当前画面恢复；旧 job 自动归档及窗口重绑不在此恢复流程中。
