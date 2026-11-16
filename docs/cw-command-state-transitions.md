# 货币战争命令的副作用与状态转换

适用于 `cw.stage.opening`、`cw.battle.run` 和 `cw.event.reconcile`。`现行` 表示沿用的行为，`本轮` 标记此次需要锁定或纠正的分支。阶段以命令结束时能确认的页面为准。战报单独记上一场战斗的轮次，整局失败回包仍以 `game_over` 表达已确认的结算。

## 共用的请求与会话语义

| 情况 | 实际输入与会话 | CLI、RPC 与恢复 |
| --- | --- | --- |
| 入口预检失败 | 无游戏输入。保留先前的档案、战报与可信阶段。 | 回传原业务错误，journal 为 `failed_before_side_effect`。 |
| 完成或仅采到部分事实 | 如实保存已验证的动作结果。未知字段维持 `unknown` 或 `null`，已有历史仍可查。 | RPC `ok=true`。使用命令已有的 `status`、`stale`、`todo`、`warn` 或 `next_action` 表达实际进度。开局、恢复命令无需新增 `status`。 |
| 操作已发送，结果未核对，或保存失败 | 停止后续盲点。已保存在磁盘的历史可以查，当前页面和未确认动作需要重新观察。 | 走现有 unknown-result 和 journal 的 `applied_but_not_persisted`。游戏状态已保存但响应未能确认时使用 `persisted_but_response_unknown`。报告现有恢复指引和 daemon taint。 |
| soft cancel 在首次输入前被观察到 | 停止，不提交业务快照。 | `REQUEST_CANCELLED`，job `cancelled`，`tainted=0`。 |
| soft cancel 在已发送或可能已发送输入后被观察到 | 停止新输入。实际页面需要复查，不能从前置页面推断已回退。 | `REQUEST_CANCEL_UNKNOWN`，job `cancel_unknown`，`tainted=1`。使用取消 token 既有的 side-effect 标记。 |
| CLI 等待预算到期 | daemon job 可以继续执行。同一运行请求或显式 request ID 复查原 job，复查不产生额外游戏动作。 | `state=running request=<id>`。它与 `cw.battle.run` 业务预算到期的 `status=in_progress` 是两个层级。 |

- `daemon.tainted` 表示上一次请求与已保存状态可能不一致。先查对应 request 的最终结果，按现有 daemon 恢复流程解除 taint，再进行有输入的 CW 命令。`cw.event.reconcile` 重新观察游戏局面，通过同一个 daemon taint 门禁。
- `cw.stage.stale`、`stage.status.stale` 及各个快照的 `stale` 表示游戏事实的有效性。阶段检测、商店报价的读取和商店仍开着是不同事实。`cw.event.reconcile` 顶层 `stale=1` 表示还有游戏事实待刷新，即使已经确认当前阶段也可以为 1。
- `run_facts` 的旧光环与投资记录可留作历史。人工接管后把当前光环标为 `unverified`，将投资历史标为可能不全。`state dump --cw-run` 仍是查完整本局档案的入口。
- 三条命令的业务失败沿用现有 CLI 封装，依靠 `fail <canonical_command> code=...` 和 RPC `ok=false` 表达。成功回包携带截图时，默认文本按协议输出 `shot` 和紧随其后的 `info read_image_first=1`。后续动作令页面发生变化或无法确认时，回包使用该分支结束时的截图，已确认的前序槽位等结构化事实仍可保留。YAML 继续按 allowlist 输出文本摘要与数据本体。

## `cw.stage.opening`

CLI 为 `trail cw stage opening --session <id>`，支持根级 `--format yaml`。只读取第二赛季开局预览，其入口锚点为同屏的「本场对局首领」「本局奇遇」和「下一步」。

| 前置或转换 | 允许的动作与停止边界 | 会话写入 | 回包 |
| --- | --- | --- | --- |
| **现行** 未识别到同屏锚点 | 只观察当前画面。不得打开详情或点「下一步」。 | 保留旧 `run_facts` 和当前阶段。 | `CW_OPENING_OVERVIEW_REQUIRED`。 |
| **现行** 已验证开局预览 | 依次打开三个首领详情，每次确认「敌方信息」后用 Esc 关闭，重新验证同一开局预览后才看下一项。打开「对手信息」，滚动读取第三位面数值，打开职级效果，再关闭并确认回预览。仅在已知词缀区域短拖以显示说明，最后清除提示并确认预览。整个流程不得点「下一步」、选择投资环境或进入战斗。 | 整段结束并验证回预览后提交 `run_facts.opening`。同一未选环境的开局重复读沿用 `run_id`。前一局已选环境或已结束时按既有规则为新预览开新档。无新的战斗或备战快照写入。 | `ok cw.stage.opening`，报告伙伴、难度、首领、词缀和已获数值。出场阶段仍为预览，回包保持原字段，不另加阶段或状态。 |
| **现行** 开局预览可核验，个别字段 OCR 不全 | 仍按上面的页面边界收口。词缀读取覆盖预览底部完整词缀区域，避开右侧「下一步」。某词缀读不到详情时不反复提交输入，也不补造效果。 | 保存已确认的首领、名称及数值。词缀保留原名，`description=null`。 | 成功回包保留该词缀，文本详情为 `unknown`，YAML 为 `null`。 |
| **本轮** 打开详情、关闭或返回预览未确认 | 在有正面页面证据前，不继续点下一张详情、盲按 Esc 或翻页。 | 本次开局档案尚未提交，保留已保存旧档。`cw.stage.opening` 的临时 UI 输入走现有 mutation journal。经验证回到原预览的正常读取保留原有装备快照有效性。 | 无输入时按原错误失败。输入已送达且结果不明时，RPC 和 journal 使用通用未知结果，保留原错误的诊断信息。 |
| **本轮** 等待页面或详情中收到取消 | 轮询期间及时观察取消。取消已生效后不再发送下一次点击、拖动或按键。 | 取消前未完成全段核对时不覆盖旧开局档案。 | 按是否已发生输入区分 clean/unknown cancel，无成功开局快照。 |

## `cw.battle.run`

CLI 为 `trail cw battle run --session <id>`，支持根级 `--format yaml`。下表的动作均以当前页面的证据为前提。`--no-auto-collect` 省略普通备战晶矿、阵容、商店、装备采集，仍可观察阶段、投影下一节点和核对光环。

| 前置或转换 | 允许的动作与停止边界 | 会话写入 | 回包 |
| --- | --- | --- | --- |
| **现行** 开场未知，且无续战线索 | 只观察，零游戏输入。已有续战提示、当前请求已经出战或已进入战斗链时允许继续被动等待。 | 未识别时不提交新战斗或阶段。 | 无续战线索时 `CW_BATTLE_STATE_UNKNOWN`。 |
| **本轮** 普通备战，尚未进入战斗链 | 同一帧确认出战控件后允许发出一次出战输入。出战后只观察等待确认。既有出战人数不足弹窗的取消处理仍适用。 | 发出输入后记录待确认出战。观察到实际战斗或确认已到终态时清除该提示。人工确认同一输入未生效后可用 `cw battle clear-in-progress` 清除提示。 | 等待超时时可返回 `status=in_progress stale=1`。是否已开战缺乏证据时，文本省略 `in_battle`，结构化值为空。 |
| **本轮** 可确认是「幕后茶歇」备战棋盘 | 只观察。无出战、特殊节点输入或普通备战采集。 | 记录实测阶段和能识别的当前节点，清除旧续战及待确认出战提示。 | `status=observed stage=preparation in_battle=0 next_action=cw.stage.read`。存在上一场战报时保留，无普通备战 handoff。 |
| **现行** 战斗中、结算动画 | 只 OCR、截图和等待，不点击出战、选项或尚未稳定的结算按钮。 | 缓存本场可观察的进度、致命攻击及可读的扣血资料。 | 观察到超时则部分成功，保存同一场的已读战报。 |
| **现行** 结算继续页、翻页与整局失败 | 常规结算允许读血量明细，点击「继续挑战」和经核对的结算翻页按钮。发现整局失败的返回按钮时允许点「返回货币战争」，按现有规则验证回主页。未验证时保留 `returned_home=0`，不再次开局。 | 战报轮次可写入 `metrics.last_battle_round`。整局失败令 run facts `ended=true`，验证到主页才写 `entry.page=home`。 | 整局失败保留 `status=completed result=lose stage=game_over`、结算事实与是否返回主页。普通结算继续到下一稳定页或业务超时。 |
| **现行** 位面过场 | 默认识别过场后允许点击空白推进。`--stop-at-plane` 在该页停留，零过场点击。 | 停止时阶段为 `layer_transition`，保存本场已读战报。 | 主动停留为 `status=completed stage=layer_transition`。只因业务超时停留时为 `status=in_progress stage=layer_transition stale=1 in_battle=0`。两者均无普通备战 handoff。 |
| **本轮** 结算后的备战候选页（包括未读到「等级提升」） | 战后只读等待，受业务剩余预算及最多 15 秒限制。看到决策页立即停留。普通备战须至少经过 10 秒并连续两次确认升级提示已清除，才开始采集。期间允许取消，不收晶矿或开商店。 | 确认决策页后写入实际阶段，使旧备战快照失效。保留此次过渡实际观察到的节点，节点不可借用上一场轮次。超时仍未稳定时将当前阶段标为未知。 | 保留已完成的战报和战斗轮次。决策页附本页文字、当前节点或未知和 `next_action=cw.stage.read`。超时以 `stage=unknown stale=1 next_action=cw.event.reconcile` 返回。两种情形均无备战 handoff。 |
| **现行** 已结束本场并进入下一备战 | 当前调用已走过战斗链或正在续接上一场时，看到下一备战就结束该场，不点下一场出战。自动采集按晶矿、阵容、商店、装备顺序。每次可能输入前复核页面；收起商店须同帧确认覆盖层及收起控件，点击该控件。页面变化或证据不足即停止后续输入。 | 更新实际阶段和采到的下一局面快照。上一场的 `round` 与下一节点 `current_node` 各自取证。 | `status=completed`，保留已读战报。确认仍在普通备战且符合 handoff 条件时才交接 `trail-cw-prep`。晶矿拦截回包使用观察到的阶段，已有决策文本可用于核验。 |
| **本轮** 关闭自动采集，复读见下一稳定决策页 | 不采集晶矿或备战快照，不在决策页确认、选卡或自动出战。 | 确认的决策阶段同步写入 session。遇到实际决策页按既有办法使备战快照失效。 | 保留 `status=completed`、原战报及原 `round`，回包 `stage`、`current_node.stage` 和恢复指引都对应新页面，无备战 handoff。 |
| **现行** 读取下一节点的 OCR 失败 | 不以 OCR 失败换取更多游戏动作。 | 刚刚独立确认的阶段可以保留，缺失的 node/HP 以空值表达。 | 保留战报和结果。`--no-auto-collect` 路径附 `CW_CURRENT_NODE_READ_FAILED` 提示和 `next_action=cw.stage.read`。 |
| **现行** 决策页、商店等其他已确认稳定页 | 只观察。可读决策页附文字和位置，不自行选择光环、奇遇、补给、投资策略等。 | 更新实际阶段。`plot_armor` 进入选择时使旧的当前光环观察失效，历史保留。 | `status=completed` 和当前 stage。决策页给 `next_action=cw.stage.read`。本次确未读到战果时不编造 `result`。 |
| **现行** 临时打开光环详情后未确认回备战 | 仅在确认为本次打开的详情页时允许退出。处于选择或确认弹窗、页面未知时，不继续试点、不出战且不采集。 | 可信当前阶段替换旧阶段，否则清除阶段值并标 stale。相关备战 facts 失效，保留 `run_id` 和光环历史。 | 战前 `status=blocked`，战后保存 `status=completed` 及原战报。两者均给 `next_action=cw.event.reconcile`。下一节点和 HP 无证据时为未知。 |
| **现行** 业务预算到期 | 停止本次循环，在原页等待下一次调用，不启动新一场。 | 阶段缓存标 stale。仅 `in_progress/in_battle=1` 置内部 `battle_resume.in_battle_hint`；结算与过场超时不置该位。已读中间报告可缓存供同一场续读。完成结果清内部续战提示和中间报告。 | `status=in_progress`、`timeout_seconds` 和 `info next_action=cw.battle.run why=battle_flow_not_finished`。结算等待返回 `stage=settle in_battle=0`，战斗续接可为 `in_battle=1`。这类业务超时不 taint。 |
| **现行与本轮** 取消、点击或保存结果不明 | 停止后续输入。本轮让自动光环和其它已发送的实际输入也进入既有取消 journal 判定。 | 之前成功保存的战报仍可查。当前尚未成功回包的结果不能当已提交。 | 按通用规则返回失败或 unknown。已完成战斗后仅可选事实读取失败时按部分成功回包，不把战报降成请求失败。 |

## `cw.event.reconcile`

CLI 为 `trail cw event reconcile --session <id>`，使用默认文本和 daemon RPC。人工操作后先使阶段、stage/status、slots、shop、equipment、strategy、sell_plan、variable-cost roles 事实过期，再按当前页面实际核对。保留晶矿指标、已有战报和内部续战信息。回包 `attempted` 记录实际尝试，`reconciled` 只列本次确认的新鲜事实，`stale_facts` 与 `todo` 列仍待读取的内容。

| 前置或转换 | 允许的动作与停止边界 | 会话写入 | 回包 |
| --- | --- | --- | --- |
| **现行** 无可信页面，含光环退出后未知 | OCR/模板观察。缺阶段时可看 HUD 来识别战斗。无法定位时停止，不采槽位、不操作晶矿、不猜测去向。 | 阶段缓存无可信值，`stage.stale=true`，`last_stage` 清空。相关事实仍 stale，旧历史保留。 | `ok cw.event.reconcile stage=unknown stale=1`，`next_action=manual`。这属于可解释的部分结果。 |
| **现行** 普通备战 | 可临时开关光环详情。确认仍在备战后收集槽位、商店、装备。商店只为读取而打开和关闭。不得收水晶、花金币、刷新、买经验、换位或出战。 | 新鲜阵容、备战数值、装备和已确认商店快照写回，商店最终以实际关闭结果为准。策略/出售参考历史保留为 stale。 | `stage=preparation` 和本次可用事实。待刷新项仍出现在 `stale_facts/todo`。无需因为其他资料未知而抹掉 fresh facts。 |
| **本轮** 备战采集半途失败 | 槽位成功后商店打开、扫描或关闭失败时，停止后续装备采集。复查实际阶段，无页面证据时不补发关店输入。 | 保存已经确认的槽位。失败的商店保持 stale，未能确认关闭的候选报价不作为新鲜快照提交。当前阶段明确时同步写入 `last_stage`，未知时清除阶段值。 | 报告实际 `attempted=slots\|shop`、此轮确认的槽位及待刷新的商店，附 `CW_PREP_COLLECT_FAILED`。复查落到决策页时用现有决策文本和路由。 |
| **本轮** 已开商店，扫描与关闭 | 在同一截图读取商店覆盖和控件。明确决策标题优先，确认开店时扫描当前商店，即使背景还能看到出战按钮。读取后发送一次关店。关闭后最多 2 秒只读复核新页面。不能重开、补点关闭、替玩家操作决策页或继续采集备战事实。 | 确认回到普通备战且五格报价均有证据时才标为 fresh 并写 `opened=false`。落到决策页时将报价标 stale，无法确认阶段时 `opened=null`。响应 stage 与 `last_stage` 使用复核出的页面，仍在 shop 时保留 `opened=true`。 | 确认新阶段时按该阶段回包。截图取核对后的页面。决策页附已有的文本与 `next_action`，未知页使用 `manual`。 |
| **本轮** 商店报价部分或全部读不出 | 按商店裁剪区真实宽度区分五格。只对未确认格用同一张图做一次局部高精 OCR。 | 仍未查清的商店保持 stale。部分读到时保存可确认报价。全空且没有商品证据时保留已有快照。 | 报告 `CW_SHOP_SCAN_UNCERTAIN`，将商店留在 `todo`，不把空识别结果标为新鲜报价。 |
| **现行与本轮** 商店扫描失败、关闭失败或关闭后页面未知 | 扫描失败时不点击关闭。关闭调用失败或页面无法确认时停止后续输入，复查能识别的页面。 | 未确认的商店保持 stale，不列为 reconciled。阶段无法确认时清空阶段值，保留已存历史。 | 能保存已验证事实并明确指出待核对事项时返回局部成功。页面未知时为 `stage=unknown next_action=manual`。请求无法提交或响应不明时按通用 journal 规则。 |
| **现行** 已打开光环详情、选择页或其他已确认决策页 | 可读当前详情。只有本次自动从备战打开并确认的详情可自动退出。不替玩家确认重选、投资、遭遇、补给和退出弹窗。 | 保存已读光环。待决策的旧光环状态保留 `unverified`，不从历史填入。阶段仅在证据明确时写入。 | `replenish/invest/encounter/fortune/event` 路由 `cw.event.handle`，其余可读决策页例如 `plot_armor` 路由 `cw.stage.read`。可附决策文本。 |
| **现行** 战斗、结算、位面过场或整局失败 | 只观察，不在恢复命令中开始/继续战斗或翻页。 | 更新可确认阶段，仍待读取的资料保持 stale。已有战报和续战位保留。 | 已确认 `battle/settle/layer_transition/game_over` 给 `next_action=cw.battle.run`。 |
| **现行与本轮** 无输入失败或恢复时取消 | 停止后续步骤。商店、光环或槽位自动恢复阶段观察到取消后不再发出新输入。 | 已确认且保存的事实可查。取消后出现未核实的 UI 动作时不能以旧阶段表示当前页面。 | 采用共用取消与请求失败语义。局部读取失败且仍能给出可信恢复指引时可以返回 `ok=true` 和 stale/todo。 |

## 入口验收

1. 以台本式 OCR、模板响应和虚拟时间驱动真实页面动作编排。记录有序输入，断言每次场景变更只发生在允许的页面，任何不允许的动作都没有发出。
2. 通过真正的请求入口加载与重读会话。核对阶段、过期状态、`run_id`、开局档案、已知战报、续战位、journal 和 taint。将无输入失败、已输入未知和读出部分事实拆开断言。
3. 对新增分支核对正常 Typer/RPC 回包。默认文本核对 canonical 命令和必须出现的事实，YAML 继续用本体中的空值表达缺失的战报字段。输出字段是事实和恢复指引，具体运营由调用方决定。
4. 测试断言代码、CLI/RPC、renderer 与现行技能入口。离线测试使用本地 fixture，不以本文件或真实游戏作为测试输入。

光环重选按双卡或三卡的实际物理卡位编号，锁定位置不改变后续编号；仅点击指定卡一次并停在确认页。
