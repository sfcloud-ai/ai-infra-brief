# 真实晨报私有审计包 v1.3

本合同供真实云端研究任务使用，不把本地 mock runner 改成真实生成服务。研究、编辑的正常产物仍通过既有报告仓库流程发布；审计包只用于核对输入、证据、选择和失败状态。

## 启用与适用范围

正式新日报采用 v1.3 完整覆盖合同；接入时核对同版本验证器、私有目录、原宿主发布许可及调用路径。仅合入代码或完成 relay CD 不算接入成功。`scope=new_daily` 只支持历史中尚无当日报告的 revision 1 新日报。同日修订、沿用既有条目与历史替换不在首版验收范围，不能改名成新日报；它们继续走既有修订和发布校验，不能宣称本审计已覆盖。历史 v1.0—v1.2 原件仍可只读核验，新的正式日报不能降级使用历史合同。部署、接入和实际生产验收分别记录。

## 保存范围与文件绑定

审计包放在任务的**私有目录**，不加入公开 Git 仓库、PR、Pages 或通知附件。原始来源文本、淘汰候选和运行信息不会因本合同获得公开授权。不要复制私人消息、凭据或无关数据。旧报告不补造当时没有保存的证据、模型版本或读取时间。

目录包含：

- `audit.json`：符合同一版本的 `schemas/research-audit.schema.json`。
- `candidates.json`：沿用 `candidates.schema.json`，真实记录使用 `mode=live`。记录实际核验的候选，包括最后淘汰项；搜索线索或读取失败不能冒充已核验候选，保留在来源/覆盖记录中。
- `history.json`：实际读取的 `ai-infra-reports/reports/index.json` 原字节，记录对应的完整 Git commit 与 SHA-256。
- `prompts/research.md`、`editor.md`、`sources.md`、`delivery.md`、`audit.md`：同一固定 Git commit 下实际读取的原字节，各自计算 SHA-256。不要把 main 的后续内容混入本期。
- `evidence/<source_id>.txt`：实际读取到的来源正文文本的 UTF-8 字节，计算内容 SHA-256。仅保存真实读取的范围，不把搜索摘要当正文，不声称这是完整网页或独立复现实验。候选的 `evidence` 采用该文本中一段短的逐字摘录，事实归纳另写 `summary`。
- `draft.json`：待发布的真实 `report.json` 原字节，记录 SHA-256；失败且尚未形成草稿时不创建，`draft_sha256=null`。

哈希针对文件原字节，不是重新格式化后的 JSON。完成后不可改动其中单个文件而继续沿用旧结果。`audit.json` 内的 Git commit 是任务记录的读取版本，验证器不联网确认其来源，不能把本地一致性检查冒充远程版本证明。最终正文、图片、索引、敏感信息检查仍由既有报告发布门禁及实际审校负责。

验证器读取的全部绑定文件合计最多 20,000,000 字节。元数据、历史、候选、提示词和草稿各自最多 2,000,000 字节；单份来源证据可使用总包剩余额度，不受元数据的单文件上限限制。仍须校验完整原字节的 SHA-256、UTF-8 与候选摘录，不得为压到限额内截断、重新序列化或用字段投影替换已读取的原始证据。超过总包限额时保留原包并报告错误。

## 新日报完整覆盖合同

`schema_version=1.3` 必须声明 `coverage_contract=daily_required_v1`，并逐项填写以下十九个 `checks`。研究方向与来源检查相互补充，最终入选篇数不设配额。

- 六个方向：`model_serving`（模型服务与超配）、`hardware`（硬件）、`model_architecture`（模型架构）、`embodied_training`（具身训练）、`small_models`（小模型）、`inference_engines`（推理引擎）。
- 十三组来源：`vllm`、`sglang`、`deepinfer`、`vllm_blog`、`sglang_blog`、`lmsys_blog`、`deepseek_articles`、`deepinfer_articles`、`glm_blog`、`qwen_blog`、`glm_models`、`deepseek_models`、`qwen_models`。

`checked` / `no_qualifying_items` 引用对应来源组的实际成功读取。发现页、分页和时间范围由研究与内容审查核对；域名匹配不证明主题相关或窗口完整。一条引擎 PR 不能填满其他方向或博客检查。模型卡与博客分别记录，模型卡可补事实，不自动补齐文章覆盖。

`not_checked` 或遗漏必查项始终阻断新日报，不能作为“允许部分晨报”的例外。真实请求已发起但来源不可用时用 `failed`，引用累计尝试记录并填写 `failure_kind`：`transport`、`access_denied`、`unusable_response` 或 `incomplete_listing`。前两类须有真实失败源；后两类须保存实际收到的正文、空响应或已读分页的原始 UTF-8 字节与哈希，解释不能完成核查的范围。HTTP 200 空壳不等于成功研究；已失败的字节不能支撑入选事实。

采用 `coverage_policy=publish_verified_with_gaps` 时，真实失败可允许非零项部分晨报，事实、来源、去重、原预算及草稿绑定保持严格。`coverage_notice.checks` 完整列出失败项，subtitle 以 `部分晨报｜覆盖缺口：` 开头，按服务、硬件、架构、具身、小模型、引擎顺序列出受影响方向（来源组归入架构或引擎），以 `。` 结束后补充简明原因，最多 180 UTF-8 字节；相关正文逐项说明具体来源与未核验范围。未经检查不写成“无新进展”。完整覆盖时 notice 为 null。

v1.3 摘要带 `coverage_checked`、`coverage_unchecked`、全部 `required_checks`、固定输入版本/哈希（含 `inputs.draft_sha256`）和原 `limits`。原宿主核对这些绑定后，沿已授权流程继续独立内容审查、合入、Pages 验证与投递；审计程序本身只读，不新增发送动作。以下历史条款在与本节冲突时不适用于新日报。

## 研究、筛选与异常

- `sources` 对每次逻辑来源读取记录 URL、实际读取时间、可核实的发布时间、状态、尝试次数、耗时、内容哈希或错误。抓取时间不能填入发布时间；只用于覆盖核查的索引页允许 `published_at=null`，入选候选必须有可核实的事件发布时间。
- `checks.vllm`、`sglang`、`deepinfer` 分别记录三个引擎的代码变动覆盖；`vllm_blog`、`sglang_blog`、`deepseek_articles`、`deepinfer_articles` 分别记录官方博客/技术文章覆盖。每项引用真实读取记录并解释结果，不能用仓库已核查代替文章已核查。`no_qualifying_items` 需要实际成功读取，不从空候选列表推断。来源失败保留失败记录；只有明确的替代一手入口补齐了必需证据，才能将该项标为已核查。
- 每个候选恰有一条 `decisions`，写明 `selected`、`update`、`duplicate`、`insufficient_evidence` 或 `out_of_scope`、来源 ID 和具体理由。实质更新与重复项引用固定历史索引中已有的条目 ID；`update` 还应保留报告的 `update_of`，其引用必须属于同一历史 `event_key`；已报事件不能重新作为 `selected` 入报。内容比较和结论仍需研究者实际判断，不能只靠哈希判断同一事件。
- 非新闻的来源覆盖/静态规格条目单独记入 `status_entries`，绑定 `event_key`、实际读取的 `source_ids` 和说明。它们不能与新闻候选或历史事件混用，不要求伪造发布时间，不计入入选新进展数量；草稿中的每条状态记录必须绑定到成功读取的证据，不得带 `update_of`。不能把证据不足或过期新闻改名为状态条目绕过筛选。
- 开始运行前明确 `limits`：每来源/每模型阶段最多尝试次数、单操作总耗时上限、整轮时间预算。记录的尝试数包含首次请求，操作耗时包含等待和重试。不要事后提高预算来掩盖超限；按既有任务允许的有限重试执行，本验证器不会发起重试或修改调度。
- `model_calls` 分别记录真实研究和编辑阶段的结果、尝试次数、耗时及可见的模型标识。环境未暴露确切模型名或版本时使用 `null` 并填写 `unavailable_reason`，不得猜测。未完成、失败和超时不记录为成功；显式采用 v1.2 部分晨报政策时，已完成候选核验但覆盖不全的研究用下述独立 `partial` 状态。
- 合格候选为零、三家必需核查完成、研究和编辑成功且零项草稿完整时，才可给出零项成功。默认必需核查未完成会阻断；只有下述显式 v1.2 政策允许发布非零项部分晨报。真实模型失败、预算超限或缺草稿仍停在待处理状态，不进入发布步骤。

## 中断后的计时恢复

`schema_version=1.0` 保持原规则，所有操作耗时必须是实际数值。只有任务明确采用“缺失耗时告警、不单独阻止发布”政策时，才使用 `schema_version=1.1` 或继承该计时政策的 `1.2` / `1.3`：

- 原进程或计时记录丢失且无法从真实记录恢复时，来源或模型阶段的 `elapsed_seconds` 填 `null`，同时填写非空的 `timing_unavailable_reason`。不填 0、平均值、请求超时参数或整轮时间冒充实际累计耗时。已经记录的真实数值，特别是超限值，不得改成 `null`。
- 内容结果和计时分开记录。成功正文、哈希与实际读取时间仍可保留 `status=ok`；没有取得正文的请求不能因此变成成功。首次失败后重试被中断时，在错误中保留首次失败及后续结果未知的事实。尝试次数包含所有已发起请求，未知次数、缺少必需时间或缺少证据仍不能通过。
- 验证摘要通过 `warnings` 返回固定字段路径，并在有未知计时时返回 `budget_unknown=true`。这表示无法证明全部操作耗时合规，不代表零耗时或预算通过；未知耗时本身不阻断已核实内容。实测操作超限、尝试次数超限和整轮预算超限仍保持原阻断，事实、来源、覆盖、去重、成稿绑定和模型阶段要求不变。
- 此政策不授予额外重试额度。执行端在请求前持久记录尝试、起始时间和原预算，结束或中断时持久记录结果与实际耗时；限流按真实响应规定的时间退避。代理 CONNECT 拒绝、权限拒绝及原因未知的 403 不能当作普通瞬时错误自动重放；继续其他获准来源，保留具体覆盖缺口。验证器只读，不负责网络重试。

迁移已有失败包时，先保留原审计 JSON、原验证结果及文件哈希；在独立私有恢复记录中写明政策采用时间、原包位置/哈希和新验证器的完整 Git SHA。保留原研究 `prompt_commit`、五份实际使用的提示词、来源正文、读取时间、累计尝试数和原预算；不要为了匹配新验证器重写历史输入。只在新的审计记录中显式采用 v1.1 并说明真实计时缺口。新验证器版本单独记在恢复记录中，不冒充原研究版本；正式任务启用这种版本迁移后才应用于该任务。新日报仍使用同一新版本的规则与验证器。

## 历史 v1.2 的部分晨报合同

v1.0、v1.1 保持原覆盖门槛。只有任务明确采用“发布已核实条目，并公开说明覆盖缺口”的政策时，才使用 v1.2 的 `coverage_policy=publish_verified_with_gaps`。v1.2 必须填写 `coverage_policy`（也可为 `strict`）和 `coverage_notice`，不从升级版本推断政策同意。

- `checks` 继续如实记录 `failed` 或 `not_checked` 及原因，来源失败、原始错误与尝试数原样保留。已成功读取的其他来源可支撑独立核验的候选；缺失详情的候选排除，不能用标题或失败读取支撑入选结论，不能把缺口写成“没有更新”。缺口本身不授权额外请求或新一轮预算。
- 研究已真实完成现有候选的核验、选择和排除，仅因来源覆盖未完成而不能称完整研究时，`model_calls` 的 research 可写 `status=partial`、`partial_reason=coverage_incomplete`，并以非空 `error` 说明实际限制。真正未完成、崩溃、超时或输出无效的模型阶段仍用 `failed`/`timeout`，不能改名为 partial；editor 仍须成功。部分晨报至少有一个经完整证据核验的入选/更新条目，不能给零项报告套用此例外。
- `coverage_notice.checks` 必须恰好列出全部未完成的 check，无遗漏或重复；`coverage_notice.text` 是最终 report.json 的完整 `subtitle`，两者必须逐字一致。沿用报告仓库限制：最多 **180 个 UTF-8 字节**（不是 180 个中文字），不含控制字符或 `<`、`>`。开头固定为 `部分晨报｜覆盖缺口：`，依次列出未完成项的名称，以 `、` 连接并以 `。` 结束，然后补充具体缺失范围或原因。顺序及名称是：vLLM 代码、SGLang 代码、DeepInfer 代码、vLLM 博客、SGLang 博客、DeepSeek 文章、DeepInfer 文章。只列实际未完成的项。例如：`部分晨报｜覆盖缺口：vLLM 代码。后续提交页读取失败，未核验范围不代表无新进展。`
- 该 subtitle 在报告索引页可见，相关子文章正文也须说明实际范围；正文、海报与通知由原发布流程和内容审查继续核对。不要把诊断日志、代理信息或私有错误原文带入公开说明。覆盖完整时 `coverage_notice=null`，不能无故称为部分晨报。
- 结构、事实证据、时间、去重和草稿绑定要求不变；已知预算超限、实际模型失败、缺少阶段仍阻断。验证结果用 `readiness=partial`、`coverage_incomplete` 和固定字段路径的 warnings 表明范围有限；`budget_unknown` 仍只表示计时未知，不由覆盖缺口触发。退出码 0 不表示覆盖完整或已经投递。

恢复旧包仍遵守前述原件留存和版本迁移要求，在独立恢复记录中写明 v1.2 政策采用时间及新验证器 SHA。若旧研究仅因覆盖不全而被标 failed，必须基于真实已完成的候选产物说明改用 partial 的理由，并保留旧记录；不得掩盖真正执行失败。追加公开缺口说明时保存原草稿及哈希，并绑定新草稿。政策迁移/审校时间单独记录，不倒填为原研究或编辑时间；新增研究、重新执行或补充抓取继续受原累计预算约束。

## 执行验证

在已安装同一版本项目及锁定依赖的 Linux 环境运行：

`ai-brief audit-research /absolute/private/task-directory`

该命令只读文件、不联网、不发消息、不写投递账本。返回摘要不包含证据正文或模型信息：

- 退出码 `0`：文件/记录一致，`readiness=ready`、`zero_items` 或显式部分晨报政策的 `partial`；v1.1/v1.2/v1.3 还必须保留并报告 `warnings`，不能把带计时告警的结果说成预算全部合规，也不能把 partial 说成覆盖完整。
- 退出码 `3`：审计结构有效但有运行阻断，`readiness=blocked` 和具体阻断类别。
- 退出码 `2`：文件、schema、哈希、时间、引用或成稿绑定无效，不能继续发布。

所有结果均为 `delivery_allowed=false`。退出码 0 只允许在已有授权范围内继续内容审查，不是事实真实性、完整网站核查、已公开、已合入或已送达的证明，也不新增任何外发权限。环境不能运行验证器时保留包并报告该缺口，不宣称审计已通过，不用人工改为成功。

## v1.4 分层预算与按小时接续

metadata 和 summary 必须显式绑定 `budget_policy=successful_work_hourly_continuation_v1`。

仅在对应 T4 `research_plan_v4`（或旧计划已追加宿主批准的 recovery grant）、恢复策略和验证器已部署时启用；`1.0`–`1.3` 和原计划不可事后改写。v1.4 继续 `daily_required_v1`、全部十九项 checks、事实/草稿/提示词绑定以及 partial 条件。

`limits` 精确包含 `source_attempts`、`model_attempts`（成功次数额度，各最多 2）、`source_attempt_seconds`（单次网络读取上限，最多 180 秒）、`model_stage_seconds`（成功阶段操作上限，最多 3600 秒）、`run_seconds`（累计成功操作耗时额度，最多 3600 秒），值必须与 T4 原计划或已批准 grant 所提供的有效预算逐项一致。旧计划恢复在独立归档记录采用时间、grant SHA、新验证器 SHA 与原输入 SHA，不修改原计划、原失败或原提示词。真实失败和 timeout 保留实际耗时及次数，但不扣成功额度；不能把未知结果标为失败释放额度。未知结果或未知耗时保守保留该操作原 reserved_seconds 与实际已知耗时两者中的较大额度，不补零。网络有限重试与阶段接续由 T4 原授权策略控制，按下一可执行小时 :20 恢复未完成任务，不绕过授权或无限热循环。

每条 source 和 model_calls 均须 `attempt_records`，长度等于 attempts；attempts 始终为历史实际尝试总数而非成功计费次数。顺序保留所有实际尝试。共同字段为 `status`、`elapsed_seconds`、`error`、`reserved_seconds`（原 T4 操作真实预留，必须大于零且不超过对应类型上限；恢复旧记录逐项读回，不猜测）；null 时间必须另有 `timing_unavailable_reason`。来源 status 为 ok/failed/timeout/unknown；模型还可 partial。成功 error 为空，其他状态保留具体原因。最后一项状态与累计记录一致。只能在原明确失败/超时已经核验后继续，未知结果或计时不能直接重放。权限拒绝与外部写入结果未知不按普通网络波动重试。

source 累计 elapsed_seconds 包含请求、等待和退避，不与单次网络上限混用。model 累计 elapsed_seconds 保留所有实际尝试耗时，不因以前失败累计超过成功操作上限而阻断。成功操作本身超过相应上限仍阻断；网络超时作为失败事实保留，允许其他证据合规的非零项部分晨报。started_at、finished_at 与整轮墙钟时间保持真实，跨小时等待不消耗成功研究额度。summary 的 budget_accounting 分别列出 successful_attempts/seconds、failed_attempts/seconds、unknown_attempts、reserved_unknown_seconds 和 charged_seconds；run_elapsed_seconds 是实际跨小时墙钟，不冒称模型推理耗时。

模型每次尝试另有 `input_sha256`、`checkpoint_sha256`、`result_sha256`；后两者无产物时为 null。非 null 哈希对应私有包 `stage_artifacts/<sha256>.json` 的实际原字节，并随原研究 archive 持久保存、读回。恢复保留前次输入原件与哈希；补齐正文或证据可生成新 input artifact，明确记录新增证据与输入变化，不覆盖原输入。后一次 checkpoint_sha256 必须等于前次已保存 result_sha256，前次耗时必须已知；T4 原操作已 acknowledged、原检查点 stored 且成功额度与接续窗口许可成立后才能 reserve。不得用任意文件替代前次结果，也不得重放未知结果。此验证器只审核原记录，不自行发起重试或授予新窗口。

恢复后最终成功且其余检查成立，可通过审计。最终失败、成功额度超限、editor 缺失、未检查覆盖、草稿或事实证据不足仍阻断。模型输入尚待取证时不先启动模型计时；模型返回后立即保存真实结果与结束时间，后续协调/等待/归档单独记录。
