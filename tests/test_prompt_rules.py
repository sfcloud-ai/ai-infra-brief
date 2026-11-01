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

    def test_source_catalog_has_unique_ids_and_required_coverage(self):
        text = (PROMPTS / "sources.md").read_text(encoding="utf-8")
        ids = re.findall(r"^\| `([a-z][a-z0-9_]*)` \|", text, re.MULTILINE)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue({"nvidia_gpu_specs", "vllm", "sglang", "deepinfer", "glm", "deepseek_models", "qwen",
                         "lmcache", "isaac_gr00t", "lerobot", "openpi", "gemma", "microsoft_small_models", "smol_models"} <= set(ids))
        for project in ("vllm-project/vllm", "sgl-project/sglang", "deepseek-ai/DeepInfer"):
            self.assertIn(f"https://github.com/{project}/pulls", text)
            self.assertIn(f"https://github.com/{project}/commits", text)

    def test_catalog_links_are_public_https_discovery_entries(self):
        text = (PROMPTS / "sources.md").read_text(encoding="utf-8")
        links = re.findall(r"\]\((https?://[^)]+)\)", text)
        self.assertGreater(len(links), 15)
        hosts = {"github.com", "huggingface.co", "www.nvidia.com", "developer.nvidia.com",
                 "docs.nvidia.com", "qwen.ai", "ai.google.dev"}
        for link in links:
            parsed = urlsplit(link)
            self.assertEqual(parsed.scheme, "https")
            self.assertIn(parsed.hostname, hosts)
            self.assertIsNone(parsed.username)
            self.assertIsNone(parsed.password)
            self.assertFalse(parsed.query)

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
