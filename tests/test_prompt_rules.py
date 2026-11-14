"""研究规则与官方来源入口的离线回归，不把文本检查当成真实研究。"""

from pathlib import Path
import re
import shutil
import tempfile
import unittest
from urllib.parse import urlsplit

from ai_brief.contracts import RESOURCE_ROOT
from ai_brief.pipeline import implementation_fingerprint


PROMPTS = RESOURCE_ROOT / "prompts"


class PromptRuleTests(unittest.TestCase):
    def test_deferred_pages_have_fixed_topics_and_hourly_resume(self):
        ids = ("model-serving-gpu", "hardware-status", "model-architecture-status",
               "embodied-training", "small-model-status", "inference-engines")
        for name in ("research", "editor", "sources", "audit", "delivery"):
            text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
            for article_id in ids:
                self.assertIn(article_id, text)
            for rule in ("下一小时的 :20", "不是下一自然日", "不生成占位 URL",
                         "原 report_date", "已发送页面不重做、不重发", "必须续读正文",
                         "未知上传/发送先按原操作对账"):
                self.assertIn(rule, text)
        delivery = (PROMPTS / "delivery.md").read_text(encoding="utf-8")
        self.assertIn("晨报说明：", delivery)
        self.assertIn("AI Infra 晨报 · YYYY-MM-DD · 补充", delivery)
        self.assertIn("相同页及内容哈希已送达则跳过", delivery)
        self.assertIn("不是可直接绕过现役校验的命令", delivery)

    def test_research_explicitly_requires_the_source_catalog(self):
        research = (PROMPTS / "research.md").read_text(encoding="utf-8")
        self.assertIn("必须读取", research)
        self.assertIn("[官方数据源目录 sources.md](sources.md)", research)
        self.assertIn("https://github.com/sfcloud-ai/ai-infra-brief/blob/main/ai_brief/resources/prompts/sources.md", research)
        self.assertTrue((PROMPTS / "sources.md").is_file())

    def test_priority_names_and_uncertain_hardware_are_preserved(self):
        for name in ("research.md", "editor.md"):
            text = (PROMPTS / name).read_text(encoding="utf-8")
            for term in ("H100", "H200", "Pro5500", "型号待核实", "GLM", "DeepSeek", "Qwen", "超配", "制造业具身", "SOTA", "小模型"):
                self.assertIn(term, text, f"{name}: {term}")

    def test_daily_engine_columns_do_not_imply_completed_checks(self):
        for name in ("research.md", "editor.md"):
            text = (PROMPTS / name).read_text(encoding="utf-8")
            for term in ("vLLM", "SGLang", "DeepInfer", "独立", "尚未核查", "访问失败", "open", "merged", "released"):
                self.assertIn(term, text, f"{name}: {term}")
            self.assertRegex(text, r"不[^\n。；]*凑")
        editor = (PROMPTS / "editor.md").read_text(encoding="utf-8")
        self.assertIn("不代表本地 mock renderer 已支持该版式", editor)
        self.assertIn("不能把真实研究稿伪装成 mock", editor)

    def test_delivery_uses_all_article_titles_and_one_landscape_poster(self):
        for name in ("editor.md", "delivery.md"):
            text = (PROMPTS / name).read_text(encoding="utf-8")
            self.assertIn("articles", text)
            self.assertIn("横版分区总海报", text)
            self.assertIn("子文章", text)
            self.assertIn("无编号", text)
            self.assertNotIn("长期消息只有一行本期子标题", text)
        delivery = (PROMPTS / "delivery.md").read_text(encoding="utf-8")
        self.assertIn("文字不另加各篇摘要", delivery)
        self.assertIn("notice.json", delivery)
        self.assertIn("访问失败保留消息和游标", delivery)

    def test_real_prompts_require_private_audit_without_public_evidence(self):
        for name in ("research.md", "editor.md"):
            text = (PROMPTS / name).read_text(encoding="utf-8")
            self.assertIn("[私有审计合同 audit.md](audit.md)", text)
            self.assertIn("audit-research", text)
            self.assertIn("schema_version=1.3", text)
            self.assertIn("coverage_contract=daily_required_v1", text)
            self.assertIn("同日修订继续原修订流程", text)
            self.assertIn("私有任务目录", text)
            self.assertIn("not_checked", text)
            self.assertIn("原授权继续", text)
        audit = (PROMPTS / "audit.md").read_text(encoding="utf-8")
        self.assertIn("unavailable_reason", audit)
        self.assertIn("delivery_allowed=false", audit)
        self.assertIn("不补造", audit)

    def test_public_url_rules_keep_one_current_page_and_internal_identity(self):
        for name in ("editor.md", "delivery.md"):
            text = (PROMPTS / name).read_text(encoding="utf-8")
            for term in ("每天只有一份当前网页", "/YYYY-MM-DD/", "/YYYY-MM-DD/<article-id>/",
                         "不含 `/rN/`", "兼容跳转", "revision", "request_id", "发送账本",
                         "revision_notice", "correction", "material_update", "maintenance", "简短", "未知"):
                self.assertIn(term, text, f"{name}: {term}")
        delivery = (PROMPTS / "delivery.md").read_text(encoding="utf-8")
        notice = re.search(r"```text\n(.*?)\n```", delivery, re.DOTALL).group(1)
        links = re.findall(r"^https://[^\s]+$", notice, re.MULTILINE)
        self.assertEqual(links, [
            "https://sfcloud-ai.github.io/ai-infra-reports/YYYY-MM-DD/model-serving-gpu/",
        ])
        self.assertNotIn("新 `rN` 是独立版次", delivery)
        for term in ("noindex", "canonical", "不保留旧正文", "已移除文章跳到该日期总页",
                     "证据读取禁止重定向", "历史原包及旧回执不改写", "未知写入仍禁止自动重发",
                     "当前报告源/清单", "不是新稳定 URL 发布能力", "scheduled_delivery"):
            self.assertIn(term, delivery)

    def test_source_catalog_has_unique_ids_and_required_coverage(self):
        text = (PROMPTS / "sources.md").read_text(encoding="utf-8")
        ids = re.findall(r"^\| `([a-z][a-z0-9_]*)` \|", text, re.MULTILINE)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue({"nvidia_gpu_specs", "vllm", "sglang", "deepinfer", "glm", "deepseek_models", "qwen",
                         "lmcache", "isaac_gr00t", "lerobot", "openpi", "gemma", "microsoft_small_models", "smol_models",
                         "vllm_blog", "sglang_blog", "deepseek_research", "deepseek_infra_articles"} <= set(ids))
        for project in ("vllm-project/vllm", "sgl-project/sglang", "deepseek-ai/DeepInfer"):
            self.assertIn(f"https://github.com/{project}/pulls", text)
            self.assertIn(f"https://github.com/{project}/commits", text)

    def test_catalog_links_are_public_https_discovery_entries(self):
        text = (PROMPTS / "sources.md").read_text(encoding="utf-8")
        links = re.findall(r"\]\((https?://[^)]+)\)", text)
        self.assertGreater(len(links), 15)
        hosts = {"github.com", "huggingface.co", "www.nvidia.com", "developer.nvidia.com",
                 "docs.nvidia.com", "qwen.ai", "ai.google.dev", "vllm.ai", "www.sglang.io",
                 "www.lmsys.org", "www.deepseek.com", "z.ai", "qwenlm.github.io"}
        for link in links:
            parsed = urlsplit(link)
            self.assertEqual(parsed.scheme, "https")
            self.assertIn(parsed.hostname, hosts)
            self.assertIsNone(parsed.username)
            self.assertIsNone(parsed.password)
            self.assertFalse(parsed.query)

    def test_official_blog_and_technical_article_entries_are_explicit(self):
        sources = (PROMPTS / "sources.md").read_text(encoding="utf-8")
        for url in ("https://vllm.ai/blog", "https://vllm.ai/blog/archive", "https://www.sglang.io/blog",
                    "https://www.lmsys.org/blog/", "https://www.deepseek.com/news/",
                    "https://github.com/deepseek-ai/open-infra-index"):
            self.assertIn(f"]({url})", sources)
        self.assertIn("DeepInfer", sources)
        self.assertIn("FlashInfer", sources)
        self.assertIn("不是文章精确发布时间", sources)

    def test_qwen_uses_current_official_research_entry(self):
        sources = (PROMPTS / "sources.md").read_text(encoding="utf-8")
        for source_id in ("qwen_blog", "qwen"):
            row = next(line for line in sources.splitlines() if line.startswith(f"| `{source_id}` |"))
            self.assertIn("](https://qwen.ai/research)", row)
            self.assertNotIn("](https://qwen.ai/blog)", row)
        self.assertIn("历史站仅用于历史溯源", sources)
        self.assertIn("JavaScript 空壳不等于正文读取成功", sources)
        self.assertIn("不能代替博客覆盖或证明没有文章", sources)

    def test_research_prioritizes_articles_without_losing_repository_coverage(self):
        research = (PROMPTS / "research.md").read_text(encoding="utf-8")
        self.assertIn("vLLM 官方博客", research)
        self.assertIn("SGLang 官方博客", research)
        self.assertIn("DeepSeek/DeepInfer 官方技术文章", research)
        self.assertIn("优先级可以高于零散引擎变更", research)
        self.assertIn("博客/技术文章与代码变动分别记录覆盖结果", research)
        for term in ("24h", "72h", "168h", "同一事件去重", "实质", "尚未核查", "访问失败"):
            self.assertIn(term, research)

    def test_editor_can_promote_technical_articles_without_duplicate_columns(self):
        editor = (PROMPTS / "editor.md").read_text(encoding="utf-8")
        self.assertIn("高价值的引擎技术文章可以作为技术洞察主文", editor)
        self.assertIn("同一事件只出现一次", editor)
        self.assertIn("三个小节都保留", editor)
        self.assertIn("article-id=inference-engines", editor)
        self.assertIn("相对历史报道的实质增量", editor)

    def test_article_rules_preserve_metric_and_publication_time_boundaries(self):
        for name in ("research.md", "editor.md"):
            text = (PROMPTS / name).read_text(encoding="utf-8")
            for term in ("DeepInfer", "FlashInfer", "自研融合", "kernel microbenchmark", "在线服务",
                         "缓存输入 token", "接受长度", "prefill", "SLO", "比较基线",
                         "时间精度", "午夜", "时区"):
                self.assertIn(term, text, f"{name}: {term}")
            self.assertRegex(text, r"输出(?: token)? ?吞吐")
        research = (PROMPTS / "research.md").read_text(encoding="utf-8")
        self.assertIn("不能把若干路径接入概括为", research)
        self.assertIn("当前候选 schema 需要完整时间戳", research)

    def test_source_catalog_changes_affect_runtime_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            resources = Path(directory) / "resources"
            shutil.copytree(RESOURCE_ROOT, resources)
            before = implementation_fingerprint(resources)
            catalog = resources / "prompts" / "sources.md"
            catalog.write_text(catalog.read_text(encoding="utf-8") + "\n来源目录回归测试。\n", encoding="utf-8")
            self.assertNotEqual(before, implementation_fingerprint(resources))


if __name__ == "__main__":
    unittest.main()
