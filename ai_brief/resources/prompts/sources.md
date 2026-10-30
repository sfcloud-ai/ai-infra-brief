# AI Infra 官方数据源目录 v1.0

本目录供研究 Prompt 在每次真实研究前读取；是公开一手来源的发现入口，不是已经抓取的数据集。与 research.md、editor.md 一起版本管理，并读取同一仓库 ref。初次核验日期：2026-10-30；后续仍须检查页面可读性、官方归属和实际发布时间，不能把本次核验当成永久可用保证。

## 来源评估与使用方法

此前研究规则只有“官方发布、代码/PR、论文、基准”等来源类别，没有集中登记具体入口。本目录补齐重点硬件、三个推理引擎、模型架构、缓存系统、制造业具身与前沿小模型，优先少量可解释的一手源，不追求订阅数量。

- 组织主页、发布列表和模型索引用于发现；最终 `source_url` 落到具体版本、PR、commit、官方技术报告或模型卡。读取正文及必要的实现/评测附件，不能拿索引页替代事件证据。
- 官方源也可能只有营销或缺少实验条件；“官方”不是自动入选理由。二手文章只作线索，不替代一手核验；论文须确认作者/项目归属，不能把整个论文分类订阅都当相关证据。
- 静态产品页和指南用于核对规格/兼容性，不因当天读取或页面更新就算新事件。博客、PR、release、模型卡的发布时间分别核实；PR 创建、合并与发布版本的日期不可互换。
- 每次记录读取成功、无合格变化、尚未读取或读取失败；迁移/失效时回到官方入口确认，不静默替换为第三方镜像。目录不可读、网络失败与无合格新项分别处理。
- 按 research.md 的 24 → 72 → 168 小时窗口和历史去重筛选；先查三个引擎的当日实质变化，再以需求相关性选其他源。表中所有来源都不是固定产出配额。

## 官方入口与适用边界

| source_id | 一手入口 | 关注点与过滤规则 |
| --- | --- | --- |
| `nvidia_gpu_specs` | [H100](https://www.nvidia.com/en-us/data-center/h100/) · [H200](https://www.nvidia.com/en-us/data-center/h200/) | 核对 SKU、显存、互联等静态事实；实际内核/精度支持仍需对应软件版本证据。Pro5500 原词保留，型号待核实，不映射为其他产品。 |
| `nvidia_technical_blog` | [NVIDIA 技术博客](https://developer.nvidia.com/blog/) | Hopper、推理内核、显存与带宽；只选有实现或评测证据的技术变化，排除纯宣传。 |
| `nvidia_mig` | [MIG 官方指南](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/latest/) | 隔离、资源划分、硬件支持与限制；MIG 不等于显存超配。 |
| `tensorrt_llm` | [TensorRT-LLM 发布记录](https://github.com/NVIDIA/TensorRT-LLM/releases) | NVIDIA 推理优化及可替代方案；确认模型、GPU、版本和对比负载。 |
| `vllm` | [发布](https://github.com/vllm-project/vllm/releases) · [PR](https://github.com/vllm-project/vllm/pulls) · [提交](https://github.com/vllm-project/vllm/commits) | 独立每日栏目：KV/状态、调度、适配、内核、正确性及性能回归；区分提案、合并和已发布。 |
| `sglang` | [发布](https://github.com/sgl-project/sglang/releases) · [PR](https://github.com/sgl-project/sglang/pulls) · [提交](https://github.com/sgl-project/sglang/commits) | 独立每日栏目：缓存、分离部署、内核与新架构；不照抄全部 changelog。 |
| `deepinfer` | [DeepInfer 官方仓库](https://github.com/deepseek-ai/DeepInfer) · [PR](https://github.com/deepseek-ai/DeepInfer/pulls) · [提交](https://github.com/deepseek-ai/DeepInfer/commits) | 独立每日栏目：以当前 README/实现支持矩阵核对 GPU、模型和离线/在线边界；不假定有稳定发布包或完整在线 KV/调度系统。 |
| `glm` | [zai-org 官方项目](https://github.com/zai-org) · [官方模型卡](https://huggingface.co/zai-org) | 从组织入口定位具体 GLM 模型、配置、报告与实现；核对架构及引擎适配，不将全组织动态入选。 |
| `deepseek_models` | [DeepSeek 官方项目](https://github.com/deepseek-ai) · [官方模型卡](https://huggingface.co/deepseek-ai) | 注意力/缓存机制、MoE、草稿验证和新架构；模型声明不等于三引擎均已实现。 |
| `qwen` | [Qwen 官方项目](https://github.com/QwenLM) · [官方模型卡](https://huggingface.co/Qwen) · [官方博客](https://qwen.ai/blog) | 新架构、部署与小模型；旧博客已提示迁移至 qwen.ai。新站正文不可读时明确记录，并以可读的官方模型卡/代码补证，不宣称该入口已检索成功。 |
| `lmcache` | [LMCache 官方仓库](https://github.com/LMCache/LMCache) | KV 生命周期、卸载、传输及引擎集成；不能据此推断不同模型可任意共享 KV。 |
| `isaac_gr00t` | [Isaac-GR00T 官方仓库](https://github.com/NVIDIA/Isaac-GR00T) | 具身训练/数据效率与部署资源；区分实验室、仿真、真实机器人及工厂证据。 |
| `lerobot` | [LeRobot 官方仓库](https://github.com/huggingface/lerobot) | 数据采集、训练/评测闭环和策略适配；只选与制造业操作及资源效率相关的变化。 |
| `openpi` | [Physical Intelligence 官方仓库](https://github.com/Physical-Intelligence/openpi) | VLA、微调资源和数据需求；说明机器人形态、任务与实机泛化边界。 |
| `gemma` | [Gemma 官方文档与模型卡入口](https://ai.google.dev/gemma/docs) | 前沿小模型的任务能力和部署成本；核对具体版本、尺寸、许可与质量基线。 |
| `microsoft_small_models` | [Microsoft 官方模型卡](https://huggingface.co/microsoft) | 仅跟踪 Phi 等相关小模型的具体卡片/报告，避免组织级噪声；不把自称 SOTA 当结论。 |
| `smol_models` | [Hugging Face 团队官方模型卡](https://huggingface.co/HuggingFaceTB) | SmolLM/SmolVLM 等小模型与数据效率；仅权重重传或采样配置变化通常不占正文。 |

## 尚未确认或需要逐次检查的事项

- **Pro5500**：初次核验未找到能够确认该精确型号的一手产品资料；不代表证明其不存在。继续保留用户原文，在取得确切产品页前不补写规格。
- **DeepInfer**：官方项目可确认为 `deepseek-ai/DeepInfer`。硬件/模型支持和开源范围会变化，必须读取当期版本，不能将离线推理组件默认当作 vLLM/SGLang 等在线服务的等价替代。
- 本目录不新增爬虫、RSS、API 凭据、定时器或投递动作；本地 mock 仅将目录作为随包资源纳入实现指纹，不执行联网读取。真实晨报任务按 research.md 的明确要求读取目录；不得把“文件已打包”描述成“采集已接通”。
