# AI Infra 官方数据源目录 v1.2

## 六主题交付与缺页接续（2026-11-13 起的当前要求）

本节是当前交付规则：与下文历史部分晨报、状态页或整期重发描述冲突时，以本节为准。六方向、十三来源组仍须逐一真实研究；不是只选两家引擎后把其余方向写成未完成。未就绪页不阻断其他已完整核验的页面，也不生成占位 URL 或把“尚未核验”单独包装成文章。

固定主题、顺序与稳定 article-id：

| 主题标题格式 | article-id | 必查内容 |
| --- | --- | --- |
| 模型服务与 GPU 超配：具体问题或进展 | model-serving-gpu | 独立权重、KV/运行状态、池化调度、多租户隔离、成本与 SLO |
| 硬件与算力平台：具体变化及适用条件 | hardware-status | GPU/异构硬件、通信/RDMA、存储路径；原始型号与实验条件 |
| 模型与架构：具体机制变化 | model-architecture-status | GLM/DeepSeek/Qwen 等模型卡及技术文章，MoE、状态/KV、引擎适配 |
| 制造业具身智能训练：具体训练方案或进展 | embodied-training | 训练论文/正文、数据与控制流程、实验机器人/任务、复现及工厂落地条件 |
| 前沿小模型：具体模型及质量成本边界 | small-model-status | 总权重与激活参数分开，任务质量、精度、延迟/吞吐、显存、硬件及实现 |
| 推理引擎每日变动：具体工程主题 | inference-engines | 同篇 vLLM、SGLang、DeepInfer 三小节；代码与博客分别核查 |

主题是必须研究的范围，不是必须凑出的六条新闻。每页正文依次包含：实际问题与例子；本次变化及原始日期/版本；核心机制与数据/控制流程；与现有实现区别；实验硬件、调优基线、负载、指标及限制；工程成熟度与复现条件；交付价值和下一步；原始来源与具体 PR/commit。适用时配原创机制图。完整核查确无合格新项，可简述实际检索窗口与结果；未查完不能写成无新项。

发现页/列表只是入口。发现相关候选后必须续读正文、PR 说明与实际 diff、论文和模型卡，核验日期、变更状态及后续 revert；有合理候选不得仅因已有其他栏目可发而停止。引擎 release 成功不等于 commits/PR 已覆盖。入口异常时记录实际失败，在既有权限下尝试官方网页/API/raw Git、论文或项目官网等能回答同一事实的可用入口；不得绕过明确权限拒绝，也不得将未尝试记作访问失败。

无法完成的主题记为待补页：保留原 report_date、主题/article-id、已完成范围、缺失证据、具体原因、原操作和检查点、下一动作及通知回执。公开晨报说明只写简短具体原因，不暴露私有诊断。待补页不进入 articles、导航、URL 清单和海报新闻分区，不创建“未完成”空正文来放行；已有真实页面不因新一轮缺口删除或改成空壳。

“下一轮”是下一次定时任务触发（例如下一小时的 :20），不是下一自然日。沿原日期、原检查点继续缺项，已成功研究/已发送页面不重做、不重发；跨日仍保留原日期，不阻止新日报独立启动。失败不等于放弃，未知上传/发送先按原操作对账。补页通过证据、审计、Pages 校验后，仅通知本轮新完成的页，记录页级发送回执；补齐前不宣称整期研究完成。

这是实际执行必须遵循的要求，不是声称现有服务已支持。固定旧计划和原失败不改写；需要新 schema、页级通知或宿主接续支持时，先按原授权完成兼容接入与验证，不手改已绑定字节、不虚构字段、不绕过校验，也不把 Prompt 合入说成补页已经投递。

本目录供研究 Prompt 在每次真实研究前读取；是公开一手来源的发现入口，不是已经抓取的数据集。与 research.md、editor.md 一起版本管理，并读取同一仓库 ref。初次核验日期：2026-10-30；博客与技术文章入口补充核验于 2026-11-06。后续仍须检查页面可读性、官方归属和实际发布时间，不能把本次核验当成永久可用保证。

## 来源评估与使用方法

此前研究规则只有“官方发布、代码/PR、论文、基准”等来源类别，没有集中登记具体入口。本目录补齐重点硬件、三个推理引擎、模型架构、缓存系统、制造业具身与前沿小模型，优先少量可解释的一手源，不追求订阅数量。

- 组织主页、发布列表和模型索引用于发现；最终 `source_url` 落到具体官方技术文章、版本、PR、commit、技术报告或模型卡。读取正文及必要的实现/评测附件，不能拿索引页替代事件证据。
- 官方源也可能只有营销或缺少实验条件；“官方”不是自动入选理由。二手文章只作线索，不替代一手核验；论文须确认作者/项目归属，不能把整个论文分类订阅都当相关证据。
- 静态产品页和指南用于核对规格/兼容性，不因当天读取或页面更新就算新事件。博客、PR、release、模型卡的发布时间分别核实；PR 创建、合并与发布版本的日期不可互换。
- 博客/技术文章与 release/PR/commit 分开记录覆盖结果；只检查代码仓库不能声称博客已检查。每次记录读取成功、无合格变化、尚未读取或读取失败；迁移/失效时回到官方入口确认，不静默替换为第三方镜像。目录不可读、网络失败与无合格新项分别处理。
- 按 research.md 的 24 → 72 → 168 小时窗口和历史去重筛选；每次固定核查 vLLM、SGLang、LMSYS、GLM、Qwen 官方博客，DeepSeek/DeepInfer 官方技术文章、三个引擎代码变动，以及 GLM/DeepSeek/Qwen 模型入口；同时完成模型服务与超配、硬件、模型架构、具身训练、小模型、推理引擎六个方向的发现。选题按技术价值和需求相关性排序，有机制与实测的技术文章可以优先于零散 PR/commit；表中来源都不是固定产出配额。

## 官方入口与适用边界

| source_id | 一手入口 | 关注点与过滤规则 |
| --- | --- | --- |
| `nvidia_gpu_specs` | [H100](https://www.nvidia.com/en-us/data-center/h100/) · [H200](https://www.nvidia.com/en-us/data-center/h200/) | 核对 SKU、显存、互联等静态事实；实际内核/精度支持仍需对应软件版本证据。Pro5500 原词保留，型号待核实，不映射为其他产品。 |
| `nvidia_technical_blog` | [NVIDIA 技术博客](https://developer.nvidia.com/blog/) | Hopper、推理内核、显存与带宽；只选有实现或评测证据的技术变化，排除纯宣传。 |
| `nvidia_mig` | [MIG 官方指南](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/latest/) | 隔离、资源划分、硬件支持与限制；MIG 不等于显存超配。 |
| `tensorrt_llm` | [TensorRT-LLM 发布记录](https://github.com/NVIDIA/TensorRT-LLM/releases) | NVIDIA 推理优化及可替代方案；确认模型、GPU、版本和对比负载。 |
| `vllm_blog` | [vLLM 官方博客](https://vllm.ai/blog) · [完整归档](https://vllm.ai/blog/archive) | 固定核查机制、算子/库接入、融合、并行与服务性能技术文章；从正文追溯实现和 benchmark。当前文章可经页面的 View Markdown Source 核对 front matter 发布时间，不用 Git 提交时间代替。 |
| `sglang_blog` | [SGLang 官方博客](https://www.sglang.io/blog) · [LMSYS 历史博客](https://www.lmsys.org/blog/) | 固定核查内核融合、推测解码、缓存与端到端服务分析；历史入口作为补充，需确认作者与项目归属。区分真实生成、固定接受长度模拟和只计 decode 的测量。 |
| `lmsys_blog` | [LMSYS 博客](https://www.lmsys.org/blog/) | 与 SGLang 当前博客分别核查、按事件去重；确认作者和项目归属。 |
| `deepseek_research` | [DeepSeek 研究与动态](https://www.deepseek.com/news/) | 固定核查官方技术文章、模型机制和发布说明，沿正文读取原始论文与实现。API 生效时间不等于文章发布时间，发布权重不自动入选。 |
| `deepseek_infra_articles` | [DeepSeek 官方基础设施文章索引](https://github.com/deepseek-ai/open-infra-index) | 固定核查系统设计、算子库与推理基础设施文章，沿索引进入具体论文、技术说明和官方项目；索引年月或仓库更新时间不是文章精确发布时间。 |
| `vllm` | [发布](https://github.com/vllm-project/vllm/releases) · [PR](https://github.com/vllm-project/vllm/pulls) · [提交](https://github.com/vllm-project/vllm/commits) | 统一引擎文章内的小节：KV/状态、调度、适配、内核、正确性及性能回归；区分提案、合并和已发布。 |
| `sglang` | [发布](https://github.com/sgl-project/sglang/releases) · [PR](https://github.com/sgl-project/sglang/pulls) · [提交](https://github.com/sgl-project/sglang/commits) | 统一引擎文章内的小节：缓存、分离部署、内核与新架构；不照抄全部 changelog。 |
| `deepinfer` | [DeepInfer 官方仓库](https://github.com/deepseek-ai/DeepInfer) · [PR](https://github.com/deepseek-ai/DeepInfer/pulls) · [提交](https://github.com/deepseek-ai/DeepInfer/commits) | 统一引擎文章内的小节及官方技术说明入口：以当前 README、关联技术文章和实现支持矩阵核对 GPU、模型和离线/在线边界；不假定有稳定发布包或完整在线 KV/调度系统。 |
| `glm_blog` | [Z.ai 官方技术文章](https://z.ai/blog) · [官方站点目录](https://z.ai/sitemap.xml) | 与模型卡分别核查。入口返回 404、动态空壳或目录不完整时保留实际失败；从官方目录定位并读取具体文章。搜索摘要只作线索，旧文章不能证明当前窗口查全。 |
| `glm` | [zai-org 官方项目](https://github.com/zai-org) · [官方模型卡](https://huggingface.co/zai-org) | 从组织入口定位具体 GLM 模型、配置、报告与实现；核对架构及引擎适配，不将全组织动态入选。 |
| `deepseek_models` | [DeepSeek 官方项目](https://github.com/deepseek-ai) · [官方模型卡](https://huggingface.co/deepseek-ai) | 注意力/缓存机制、MoE、草稿验证和新架构；模型声明不等于三引擎均已实现。 |
| `qwen_blog` | [Qwen 官方博客](https://qwen.ai/research) · [历史官方博客](https://qwenlm.github.io/blog/) | 独立记录当前窗口的文章覆盖。2026-11-14 核验旧站官方迁移链接指向 qwen.ai/research；历史站仅用于历史溯源。HTTP 200 的 JavaScript 空壳不等于正文读取成功；须取得具体文章正文及日期，新站不可读时保留实际失败，模型卡只能补模型事实，不能代替博客覆盖或证明没有文章。 |
| `qwen` | [Qwen 官方项目](https://github.com/QwenLM) · [官方模型卡](https://huggingface.co/Qwen) · [官方博客](https://qwen.ai/research) | 新架构、部署与小模型；旧博客已提示迁移至 qwen.ai。新站正文不可读时明确记录，并以可读的官方模型卡/代码补证，不宣称该入口已检索成功。 |
| `lmcache` | [LMCache 官方仓库](https://github.com/LMCache/LMCache) | KV 生命周期、卸载、传输及引擎集成；不能据此推断不同模型可任意共享 KV。 |
| `isaac_gr00t` | [Isaac-GR00T 官方仓库](https://github.com/NVIDIA/Isaac-GR00T) | 具身训练/数据效率与部署资源；区分实验室、仿真、真实机器人及工厂证据。 |
| `lerobot` | [LeRobot 官方仓库](https://github.com/huggingface/lerobot) | 数据采集、训练/评测闭环和策略适配；只选与制造业操作及资源效率相关的变化。 |
| `openpi` | [Physical Intelligence 官方仓库](https://github.com/Physical-Intelligence/openpi) | VLA、微调资源和数据需求；说明机器人形态、任务与实机泛化边界。 |
| `gemma` | [Gemma 官方文档与模型卡入口](https://ai.google.dev/gemma/docs) | 前沿小模型的任务能力和部署成本；核对具体版本、尺寸、许可与质量基线。 |
| `microsoft_small_models` | [Microsoft 官方模型卡](https://huggingface.co/microsoft) | 仅跟踪 Phi 等相关小模型的具体卡片/报告，避免组织级噪声；不把自称 SOTA 当结论。 |
| `smol_models` | [Hugging Face 团队官方模型卡](https://huggingface.co/HuggingFaceTB) | SmolLM/SmolVLM 等小模型与数据效率；仅权重重传或采样配置变化通常不占正文。 |

## 尚未确认或需要逐次检查的事项

- **Pro5500**：初次核验未找到能够确认该精确型号的一手产品资料；不代表证明其不存在。继续保留用户原文，在取得确切产品页前不补写规格。
- **DeepInfer**：官方项目可确认为 `deepseek-ai/DeepInfer`，与 FlashInfer 是不同项目；按原文辨认外部库接入和引擎自研融合，不擅自替换项目名。硬件/模型支持和开源范围会变化，必须读取当期版本，不能将离线推理组件默认当作 vLLM/SGLang 等在线服务的等价替代。
- **发布时间精度**：部分 SGLang/DeepSeek 正文只公开日期，部分基础设施索引只公开年月。先查官方文章元数据或原始发布记录；无法确认带时区时间时保留已知精度并记为待核验线索，不编造午夜、时区，不以检索时间替代。当前候选 schema 要求完整时间戳，未补齐前不得伪造字段纳入候选，也不能把访问成功但时间待核验说成无合格新项。
- 本目录不新增爬虫、RSS、API 凭据、定时器或投递动作；本地 mock 仅将目录作为随包资源纳入实现指纹，不执行联网读取。真实晨报任务按 research.md 的明确要求读取目录；不得把“文件已打包”描述成“采集已接通”。
