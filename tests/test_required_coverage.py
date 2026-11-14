"""完整研究范围的合成回归；不联网，不把测试样本当作生产核验。"""
import hashlib
import unittest

from ai_brief.coverage import CONTRACT, REQUIRED_CHECKS, notice_prefix, source_matches
from tests import test_research_audit as fixtures


class RequiredCoverageTests(unittest.TestCase):
    save = fixtures.ResearchAuditTests.save
    check = fixtures.ResearchAuditTests.check
    reject = fixtures.ResearchAuditTests.reject

    def setUp(self):
        fixtures.ResearchAuditTests.setUp(self)
        urls = {
            "model_serving": "https://arxiv.org/search/?query=multi-model+serving",
            "hardware": "https://developer.nvidia.com/blog/",
            "model_architecture": "https://huggingface.co/Qwen",
            "embodied_training": "https://github.com/Physical-Intelligence/openpi",
            "small_models": "https://huggingface.co/HuggingFaceTB",
            "inference_engines": "https://github.com/vllm-project/vllm/releases",
            "vllm": "https://api.github.com/repos/vllm-project/vllm/commits",
            "sglang": "https://api.github.com/repos/sgl-project/sglang/commits",
            "deepinfer": "https://github.com/deepseek-ai/DeepInfer/commits",
            "vllm_blog": "https://vllm.ai/blog/archive",
            "sglang_blog": "https://www.sglang.io/blog",
            "lmsys_blog": "https://www.lmsys.org/blog/",
            "deepseek_articles": "https://www.deepseek.com/news/",
            "deepinfer_articles": "https://github.com/deepseek-ai/open-infra-index",
            "glm_blog": "https://z.ai/blog", "qwen_blog": "https://qwen.ai/blog",
            "glm_models": "https://huggingface.co/api/models?author=zai-org",
            "deepseek_models": "https://huggingface.co/api/models?author=deepseek-ai",
            "qwen_models": "https://huggingface.co/api/models?author=Qwen",
        }
        self.audit.update(schema_version="1.3", coverage_contract=CONTRACT,
                          coverage_policy="publish_verified_with_gaps", coverage_notice=None)
        self.audit["checks"] = {}
        for name in REQUIRED_CHECKS:
            identity = "coverage-" + name
            data = ("合成官方发现响应：" + name).encode()
            self.root.joinpath("evidence", identity + ".txt").write_bytes(data)
            self.audit["sources"].append(dict(self.audit["sources"][0], id=identity, url=urls[name],
                                               content_sha256=hashlib.sha256(data).hexdigest(), elapsed_seconds=1))
            self.audit["checks"][name] = {"status": "checked", "source_ids": [identity], "reason": "合成完整覆盖证据"}

    def failed(self, name="qwen_blog", kind="access_denied", retained=None):
        source = next(s for s in self.audit["sources"] if s["id"] == "coverage-" + name)
        source.update(status="failed", content_sha256=None, error="合成来源响应失败")
        if retained is not None:
            self.root.joinpath("evidence", source["id"] + ".txt").write_bytes(retained)
            source["content_sha256"] = hashlib.sha256(retained).hexdigest()
        self.audit["checks"][name].update(status="failed", failure_kind=kind)
        gaps = [key for key, value in self.audit["checks"].items() if value["status"] == "failed"]
        text = notice_prefix(gaps) + "来源读取受限，缺口不代表无新进展。"
        self.audit["coverage_notice"] = {"checks": gaps, "text": text}
        self.draft["subtitle"] = text
        return source

    def test_complete_contract_binds_all_directions_sources_and_original_inputs(self):
        result = self.check()
        self.assertEqual(result["readiness"], "ready")
        self.assertEqual(result["coverage_contract"], CONTRACT)
        self.assertEqual(set(result["required_checks"]), set(REQUIRED_CHECKS))
        self.assertEqual(set(result["coverage_checked"]), set(REQUIRED_CHECKS))
        self.assertEqual(result["coverage_unchecked"], [])
        self.assertEqual(result["inputs"]["prompt_hashes"], self.audit["inputs"]["prompt_hashes"])
        self.assertEqual(result["inputs"]["history_sha256"], self.audit["inputs"]["history_sha256"])
        self.assertEqual(result["limits"], self.audit["limits"])
        self.assertEqual(result["inputs"]["draft_sha256"], self.audit["inputs"]["draft_sha256"])

    def test_qwen_research_entry_and_articles_are_valid_audit_sources(self):
        source = next(s for s in self.audit["sources"] if s["id"] == "coverage-qwen_blog")
        for url in ("https://qwen.ai/research", "https://qwen.ai/research/",
                    "https://www.qwen.ai/research/example", "https://qwen.ai/blog",
                    "https://qwenlm.github.io/blog/example"):
            source["url"] = url
            self.assertEqual(self.check()["readiness"], "ready", url)
        source = self.failed(kind="unusable_response", retained=b"<html>JavaScript shell</html>")
        source["url"] = "https://qwen.ai/research"
        self.assertEqual(self.check()["readiness"], "partial")
        for url in ("https://qwen.ai/research-other", "https://qwen.ai/", "https://qwen.ai/chat",
                    "https://qwen.ai.example/research", "https://qwenlm.github.io/research"):
            self.assertFalse(source_matches("qwen_blog", url), url)
        self.assertFalse(source_matches("glm_blog", "https://qwen.ai/research"))

    def test_every_required_direction_and_source_group_must_exist(self):
        for name in REQUIRED_CHECKS:
            value = self.audit["checks"].pop(name)
            with self.subTest(check=name):
                self.reject("metadata schema")
            self.audit["checks"][name] = value

    def test_not_checked_blocks_even_with_the_partial_report_policy(self):
        for name in REQUIRED_CHECKS:
            self.audit["checks"][name]["status"] = "not_checked"
            result = self.check()
            self.assertEqual(result["readiness"], "blocked")
            self.assertIn("required_coverage_not_checked", result["blockers"])
            self.assertEqual(result["coverage_unchecked"], [name])
            self.audit["checks"][name]["status"] = "checked"

    def test_an_engine_source_cannot_fill_model_or_blog_checks(self):
        for name in ("small_models", "hardware", "qwen_blog", "glm_models", "lmsys_blog"):
            before = self.audit["checks"][name]["source_ids"]
            self.audit["checks"][name]["source_ids"] = ["coverage-vllm"]
            self.reject("required source group")
            self.audit["checks"][name]["source_ids"] = before

    def test_real_attempt_failure_allows_disclosed_verified_partial_report(self):
        self.failed()
        result = self.check()
        self.assertEqual(result["readiness"], "partial")
        self.assertEqual(result["coverage_incomplete"], ["qwen_blog"])
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(result["coverage_unchecked"], [])

    def test_unattempted_failure_cannot_masquerade_as_a_coverage_gap(self):
        source = self.failed(); source["attempts"] = 0
        self.reject("metadata schema")
        source["attempts"] = 1; self.audit["checks"]["qwen_blog"]["source_ids"] = []
        self.reject("attempted matching source")

    def test_empty_or_unusable_response_retains_bytes_without_becoming_successful_evidence(self):
        for data in (b"", b"<html><body>JavaScript required</body></html>"):
            self.failed(kind="unusable_response", retained=data)
            self.assertEqual(self.check()["readiness"], "partial")

    def test_real_timeout_remains_a_transport_gap_with_original_budget(self):
        source = self.failed(kind="transport"); source["status"] = "timeout"
        self.assertEqual(self.check()["readiness"], "partial")
        self.assertEqual(source["status"], "timeout")
        source["attempts"] = self.audit["limits"]["source_attempts"] + 1
        self.assertIn("source_budget_exceeded", self.check()["blockers"])

    def test_official_raw_sources_and_exact_repository_search_keep_actual_urls(self):
        for check, url in (
            ("deepinfer_articles", "https://raw.githubusercontent.com/deepseek-ai/open-infra-index/main/README.md"),
            ("deepinfer_articles", "https://raw.githubusercontent.com/deepseek-ai/DeepInfer/main/README.md"),
            ("vllm", "https://api.github.com/search/issues?q=repo%3Avllm-project%2Fvllm%20is%3Apr%20merged%3A2026-11-11T02%3A00%3A00Z..2026-11-12T02%3A00%3A00Z&sort=updated&order=desc&per_page=100"),
        ):
            self.assertTrue(source_matches(check, url), url)
        for url in (
            "https://raw.githubusercontent.com/other/open-infra-index/main/README.md",
            "https://api.github.com/search/issues?q=is%3Apr",
            "https://api.github.com/search/issues?q=repo%3Avllm-project%2Fvllm%20repo%3Aother%2Frepo",
            "https://api.github.com/search/issues?q=repo%3Avllm-project%2Fvllm%20OR%20is%3Apr",
            "https://api.github.com/search/issues?q=-repo%3Avllm-project%2Fvllm",
            "https://api.github.com/search/issues?q=repo%3Avllm-project%2Fvllm&q=is%3Apr",
        ):
            self.assertFalse(source_matches("vllm", url), url)

    def test_documented_engine_blogs_and_tensorrt_can_support_serving_research(self):
        for url in ("https://vllm.ai/blog/archive", "https://www.sglang.io/blog",
                    "https://www.lmsys.org/blog/", "https://github.com/NVIDIA/TensorRT-LLM/releases"):
            self.assertTrue(source_matches("model_serving", url), url)

    def test_first_party_alternatives_keep_the_original_evidence_identity(self):
        for name, url in (
            ("deepseek_articles", "https://api-docs.deepseek.com/news/news250922/"),
            ("small_models", "https://huggingface.co/google/gemma-3-1b-it"),
            ("qwen_models", "https://raw.githubusercontent.com/QwenLM/Qwen3/main/README.md"),
            ("glm_models", "https://raw.githubusercontent.com/zai-org/GLM-4/main/README.md"),
            ("deepseek_models", "https://raw.githubusercontent.com/deepseek-ai/DeepSeek-V3/main/README.md"),
        ):
            source = next(s for s in self.audit["sources"] if s["id"] == "coverage-" + name)
            original = source["url"]; source["url"] = url
            self.assertEqual(self.check()["readiness"], "ready", url)
            source["url"] = original
        for url in ("https://raw.githubusercontent.com/QwenLM/Qwen3",
                    "https://raw.githubusercontent.com/QwenLM-other/Qwen3/main/README.md",
                    "https://raw.githubusercontent.com/QwenLM/../main/README.md"):
            self.assertFalse(source_matches("qwen_models", url), url)
        self.assertFalse(source_matches("small_models", "https://huggingface.co/google-other/gemma"))
        self.assertFalse(source_matches("deepseek_articles", "https://api-docs.deepseek.com/guides/chat"))

    def test_code_search_cannot_replace_deepinfer_article_coverage(self):
        for repository in ("deepseek-ai/DeepInfer", "deepseek-ai/open-infra-index"):
            for suffix in ("/pulls", "/issues/1", "/commits"):
                self.assertFalse(source_matches("deepinfer_articles", "https://github.com/" + repository + suffix))
            url = "https://api.github.com/search/issues?q=repo%3A" + repository + "%20is%3Apr"
            self.assertFalse(source_matches("deepinfer_articles", url))

    def test_github_owner_and_repository_casing_does_not_rewrite_evidence(self):
        for check, url in (
            ("deepinfer", "https://github.com/DEEPSEEK-AI/deepinfer/commits"),
            ("deepinfer", "https://api.github.com/search/issues?q=repo%3Adeepseek-ai%2Fdeepinfer%20is%3Apr"),
            ("qwen_models", "https://raw.githubusercontent.com/qwenlm/Qwen3/main/README.md"),
            ("deepinfer_articles", "https://raw.githubusercontent.com/DeepSeek-AI/DeepInfer/main/README.md"),
        ):
            source = next(s for s in self.audit["sources"] if s["id"] == "coverage-" + check)
            before = source["url"]; source["url"] = url
            self.assertEqual(self.check()["readiness"], "ready")
            self.assertEqual(source["url"], url)
            source["url"] = before

    def test_unusable_bytes_must_belong_to_failed_source_but_partial_listing_can_be_usable(self):
        source = self.failed(kind="unusable_response", retained=b"unusable response")
        source.update(status="ok", error="")
        self.reject("retained failed-source bytes")
        self.audit["checks"]["qwen_blog"]["failure_kind"] = "incomplete_listing"
        self.assertEqual(self.check()["readiness"], "partial")

    def test_official_vllm_blog_markdown_and_listing_are_coverage_evidence(self):
        source = next(s for s in self.audit["sources"] if s["id"] == "coverage-vllm_blog")
        for url in (
            "https://github.com/vllm-project/vllm-project.github.io/blob/main/_posts/example.md",
            "https://raw.githubusercontent.com/vllm-project/vllm-project.github.io/main/_posts/example.md",
            "https://api.github.com/repos/vllm-project/vllm-project.github.io/contents/_posts",
        ):
            source["url"] = url
            self.assertEqual(self.check()["readiness"], "ready", url)
        for url in (
            "https://github.com/vllm-project/vllm/pull/1",
            "https://github.com/other/vllm-project.github.io/blob/main/_posts/example.md",
            "https://github.com/vllm-project/vllm-project.github.io/pull/1",
            "https://api.github.com/search/issues?q=repo%3Avllm-project%2Fvllm-project.github.io",
        ):
            self.assertFalse(source_matches("vllm_blog", url), url)

    def test_many_source_gaps_fit_existing_public_subtitle_limit(self):
        for name in REQUIRED_CHECKS:
            self.failed(name)
        self.assertLessEqual(len(self.draft["subtitle"].encode()), 180)
        self.assertEqual(len(self.check()["coverage_incomplete"]), len(REQUIRED_CHECKS))

    def test_zero_news_still_requires_complete_research_without_article_quotas(self):
        self.candidates["items"] = []; self.audit["decisions"] = []; self.draft["entries"] = []
        for check in self.audit["checks"].values():
            check["status"] = "no_qualifying_items"
        self.assertEqual(self.check()["readiness"], "zero_items")

    def test_missing_timing_warns_but_known_retry_overrun_still_blocks(self):
        source = self.audit["sources"][1]
        source.update(elapsed_seconds=None, timing_unavailable_reason="合成计时记录缺失")
        self.assertEqual(self.check()["readiness"], "ready")
        self.assertTrue(self.check()["budget_unknown"])
        source["attempts"] = self.audit["limits"]["source_attempts"] + 1
        self.assertIn("source_budget_exceeded", self.check()["blockers"])
