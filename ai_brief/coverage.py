"""每日必查范围；证据语义仍由研究与独立内容审查核对。"""

from urllib.parse import parse_qs, urlsplit

CONTRACT = "daily_required_v1"
TOPICS = {
    "model_serving": "模型服务与超配", "hardware": "硬件",
    "model_architecture": "模型架构", "embodied_training": "具身训练",
    "small_models": "小模型", "inference_engines": "推理引擎",
}
SOURCE_CHECKS = (
    "vllm", "sglang", "deepinfer", "vllm_blog", "sglang_blog", "lmsys_blog",
    "deepseek_articles", "deepinfer_articles", "glm_blog", "qwen_blog",
    "glm_models", "deepseek_models", "qwen_models",
)
REQUIRED_CHECKS = (*TOPICS, *SOURCE_CHECKS)
FAILURE_KINDS = ("transport", "access_denied", "unusable_response", "incomplete_listing")
NOTICE_LABELS = dict(zip(TOPICS, ("服务", "硬件", "架构", "具身", "小模型", "引擎")))


def notice_prefix(checks):
    """按方向压缩公开缺口标题，完整来源缺口仍保存在审计记录中。"""
    groups = {name if name in TOPICS else ("model_architecture" if name in
              {"glm_blog", "qwen_blog", "glm_models", "deepseek_models", "qwen_models", "deepseek_articles"}
              else "inference_engines") for name in checks}
    return "部分晨报｜覆盖缺口：" + "、".join(NOTICE_LABELS[name] for name in TOPICS if name in groups) + "。"


def source_matches(check, url):
    """限定对应官方来源组，避免拿一条引擎 PR 填满模型与博客检查。"""
    parts = urlsplit(url)
    host, path = parts.hostname, parts.path.rstrip("/")
    repo = {"vllm": "vllm-project/vllm", "sglang": "sgl-project/sglang",
            "deepinfer": "deepseek-ai/DeepInfer"}

    def github(repository):
        prefix = ("/repos/" if host == "api.github.com" else "/") + repository
        if host == "raw.githubusercontent.com":
            prefix = "/" + repository + "/"
            suffix = path[len(prefix):]
            return path[:len(prefix)].casefold() == prefix.casefold() and len(suffix.split("/")) >= 2 and all(suffix.split("/"))
        if host == "api.github.com" and path == "/search/issues":
            query = parse_qs(parts.query).get("q", [])
            if len(query) != 1:
                return False
            words = query[0].split()
            # 搜索限定到唯一明确仓库；OR、负向或多仓库条件不能冒充完整的本仓库检索。
            repos = [word for word in words if word.lower().lstrip("-+").startswith("repo:")]
            return [word.casefold() for word in repos] == ["repo:" + repository.casefold()] and "or" not in [word.casefold() for word in words]
        return host in {"github.com", "api.github.com"} and (path.casefold() == prefix.casefold() or path[:len(prefix) + 1].casefold() == prefix.casefold() + "/")

    def model(org, github_org):
        if host == "raw.githubusercontent.com":
            segments = path.strip("/").split("/")
            return len(segments) >= 4 and segments[0].casefold() == github_org.casefold() and all(segments) and all(
                segment not in {".", ".."} for segment in segments)
        return (host == "huggingface.co" and any(path == p or path.startswith(p + "/")
                for p in ("/" + org, "/api/models/" + org))) or (
                host == "huggingface.co" and path == "/api/models" and
                parse_qs(parts.query).get("author") == [org]) or (
                host in {"github.com", "api.github.com"} and any(path.casefold() == p.casefold() or path[:len(p) + 1].casefold() == p.casefold() + "/"
                for p in ("/" + github_org, "/orgs/" + github_org, "/repos/" + github_org)))

    if check in repo:
        return github(repo[check])
    if check in {"vllm_blog", "sglang_blog", "lmsys_blog", "glm_blog", "qwen_blog"}:
        if check == "vllm_blog" and github("vllm-project/vllm-project.github.io"):
            return not any(segment in path.casefold().split("/") for segment in
                           ("search", "issues", "issue", "pulls", "pull", "commits", "commit", "releases"))
        domains = {"vllm_blog": {"vllm.ai", "blog.vllm.ai"}, "sglang_blog": {"sglang.io", "www.sglang.io"},
                   "lmsys_blog": {"lmsys.org", "www.lmsys.org"}, "glm_blog": {"z.ai", "www.z.ai"},
                   "qwen_blog": {"qwen.ai", "www.qwen.ai", "qwenlm.github.io"}}
        return host in domains[check] and (host == "blog.vllm.ai" or path == "/blog" or path.startswith("/blog/")
                                          or (check == "glm_blog" and path == "/sitemap.xml"))
    if check == "deepseek_articles":
        return host in {"deepseek.com", "www.deepseek.com", "api-docs.deepseek.com"} and (path == "/news" or path.startswith("/news/"))
    if check == "deepinfer_articles":
        return (github("deepseek-ai/open-infra-index") or github(repo["deepinfer"])) and not any(
            segment in path.casefold().split("/") for segment in ("search", "issues", "issue", "pulls", "pull", "commits", "commit", "releases"))
    if check in {"glm_models", "deepseek_models", "qwen_models"}:
        return model(*{"glm_models": ("zai-org", "zai-org"), "deepseek_models": ("deepseek-ai", "deepseek-ai"),
                      "qwen_models": ("Qwen", "QwenLM")}[check])
    # 方向证据可来自一手论文或官方发现入口；主题相关性仍须内容审查，域名不证明语义。
    paper = host in {"arxiv.org", "export.arxiv.org"}
    nvidia = host in {"developer.nvidia.com", "docs.nvidia.com", "www.nvidia.com"}
    if check == "model_serving":
        return paper or nvidia or any(github(r) for r in (*repo.values(), "LMCache/LMCache", "NVIDIA/TensorRT-LLM")) or any(
            source_matches(c, url) for c in ("vllm_blog", "sglang_blog", "lmsys_blog", "deepinfer_articles"))
    if check == "hardware":
        return paper or nvidia
    if check == "model_architecture":
        return paper or any(source_matches(c, url) for c in ("glm_models", "deepseek_models", "qwen_models", "glm_blog", "qwen_blog", "deepseek_articles"))
    if check == "embodied_training":
        return paper or any(github(r) for r in ("NVIDIA/Isaac-GR00T", "huggingface/lerobot", "Physical-Intelligence/openpi"))
    if check == "small_models":
        return paper or host == "ai.google.dev" or any(model(*org) for org in
                (("HuggingFaceTB", "huggingface"), ("microsoft", "microsoft"), ("Qwen", "QwenLM"),
                 ("google", "google"), ("kakaocorp", "kakaocorp")))
    if check == "inference_engines":
        return any(source_matches(c, url) for c in ("vllm", "sglang", "deepinfer", "vllm_blog", "sglang_blog", "lmsys_blog"))
    return False
