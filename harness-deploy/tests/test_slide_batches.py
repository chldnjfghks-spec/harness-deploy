"""네트워크·모델 호출 없는 분할 생성 회귀 검사."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules["engine"] = types.SimpleNamespace(ROOT=ROOT)
sys.modules["roles"] = types.SimpleNamespace(
    ROOT=ROOT, load_settings=lambda: {"slides": {"model": "ollama:qwen3:1.7b"}},
    report_policy=lambda: "원문 근거를 따르세요.",
)
sys.modules["llm_client"] = types.SimpleNamespace(call_model=Mock())
import storyboard
from slide_batches import create_batched_storyboard, split_text


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.manifest = {"created": "2026-11-13", "sources": [
            {"id": "S001", "kind": "code", "path": "main.py", "text": "근거 원문\n" * 1300},
            {"id": "S002", "kind": "image", "path": "graph.png", "file": "graph.png"},
        ]}
        self.calls = []
        def respond(prompt, *args, **kwargs):
            self.calls.append(prompt)
            current = json.loads(prompt.split("현재 자료:\n", 1)[1].split("\n이전 응답 오류:", 1)[0])
            task = current["input"]
            return {"content": json.dumps({"slides": [{
                "slot_id": "overview" if task["kind"] == "report" else "data",
                "title": f'{task["kind"]} {task.get("id", "")} {task["part"]}',
                "takeaway": "제공된 자료를 정리합니다.",
                "bullets": ["첫 번째 핵심 내용", "두 번째 핵심 내용"],
                "source_ids": task["source_ids"], "notes": "원문 대조 필요",
            }]}, ensure_ascii=False)}
        self.respond = respond
        sys.modules["llm_client"].call_model = Mock(side_effect=respond)

    def tearDown(self):
        self.tmp.cleanup()

    def test_split_preserves_every_character(self):
        text = "# 장 제목\n\n" + "가나다\n" * 9000
        parts = list(split_text(text))
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(p) <= 1600 for p in parts))

    def test_long_input_budget_all_sources_and_images(self):
        report = "보고서 [S001]\n" * 2500
        plan, ids = storyboard.create_storyboard(report, self.manifest, self.out, "검사 제목", ["S002"])
        self.assertGreater(len(self.calls), 2)
        self.assertLess(max(map(len, self.calls)), 6500)
        tasks = json.loads((self.out / "batch_inputs.json").read_text(encoding="utf-8"))
        self.assertEqual("".join(t["text"] for t in tasks if t["kind"] == "report"), report)
        self.assertEqual("".join(t["text"] for t in tasks if t["kind"] == "source"), self.manifest["sources"][0]["text"])
        self.assertEqual(ids, ["S002"])
        self.assertEqual(storyboard.validate_plan(plan, self.manifest, ids), [])
        self.assertEqual(sum(s["kind"] == "agenda" for s in plan["slides"]), 1)
        before = len(self.calls)
        storyboard.create_storyboard(report, self.manifest, self.out, "검사 제목", ["S002"])
        self.assertEqual(len(self.calls), before)

    def test_truncation_is_not_success(self):
        sys.modules["llm_client"].call_model = Mock(return_value={"content": "{}", "output_truncated": True})
        with self.assertRaisesRegex(ValueError, "출력이 잘렸"):
            create_batched_storyboard("본문", self.manifest, self.out, "검사")
        self.assertFalse((self.out / "storyboard.json").exists())

    def test_invalid_json_retries_without_growing_prompt(self):
        sys.modules["llm_client"].call_model = Mock(return_value={"content": "invalid json"})
        with self.assertRaisesRegex(ValueError, "검증 실패"):
            create_batched_storyboard("본문", self.manifest, self.out, "검사")
        calls = sys.modules["llm_client"].call_model.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(len(c.args[0]) < 6500 for c in calls))

    def test_invented_citation_rejected(self):
        def wrong(*args, **kwargs):
            response = self.respond(*args, **kwargs)
            data = json.loads(response["content"])
            data["slides"][0]["source_ids"] = ["S999"]
            return {"content": json.dumps(data)}
        sys.modules["llm_client"].call_model = Mock(side_effect=wrong)
        with self.assertRaisesRegex(ValueError, "없는 근거"):
            create_batched_storyboard("본문", self.manifest, self.out, "검사")

    def test_partial_checkpoints_resume_after_failure(self):
        count = 0
        def interrupt(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("연결 중단")
            return self.respond(*args, **kwargs)
        sys.modules["llm_client"].call_model = Mock(side_effect=interrupt)
        with self.assertRaisesRegex(RuntimeError, "연결 중단"):
            create_batched_storyboard("본문" * 2000, self.manifest, self.out, "검사")
        self.assertTrue((self.out / "batch_0001.json").exists())
        first = self.calls[0]
        self.calls.clear()
        sys.modules["llm_client"].call_model = Mock(side_effect=self.respond)
        create_batched_storyboard("본문" * 2000, self.manifest, self.out, "검사")
        self.assertNotIn(first, self.calls)


if __name__ == "__main__":
    unittest.main()
