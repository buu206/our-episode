"""Exercise the real kit.sh summary gate without changing any product state."""
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
FUNCTION = "verified_summary() {" + (ROOT / "kit.sh").read_text().split(
    "verified_summary() {", 1
)[1].split("\n}\n", 1)[0] + "\n}\n"


class SummaryGateTest(unittest.TestCase):
    def gate(self, test_result="通过（459/459）；前端测试通过", *, mode="pending",
             smoke="待验（缺冒烟记录）", mutate=None, stale=False):
        rows = [f"是否表第 {n} 项  检查：      {value}" for n, value in (
            (2, test_result), (3, smoke), (4, "通过"), (5, "通过"),
            (6, "通过"), (10, "通过"))]
        original = "\n".join(rows) + "\n"
        if mutate:
            mutate(rows)
        output = "\n".join(rows) + "\n"
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            evidence = folder / "docs/evidence/阶段3-1"
            evidence.mkdir(parents=True)
            (evidence / "verify-输出.txt").write_text(original if stale else output)
            (folder / "stdout.txt").write_text(output)
            result = subprocess.run(["bash", "-c", FUNCTION +
                '\nP="$1"; verified_summary "$1/stdout.txt" 阶段3-1 "$2"',
                "summary-test", temp, mode], capture_output=True)
        return result.returncode == 0

    def test_backend_and_frontend_success(self):
        self.assertTrue(self.gate())

    def test_backend_only_success(self):
        self.assertTrue(self.gate("通过（459/459）"))

    def test_real_smoke_success(self):
        self.assertTrue(self.gate(mode="real", smoke="有记录（1 次调用）"))

    def test_frontend_cannot_hide_backend_failure(self):
        self.assertFalse(self.gate("不通过（458/459）；前端测试通过"))

    def test_frontend_failure_is_rejected(self):
        self.assertFalse(self.gate("不通过（前端测试退出码 1）"))

    def test_unknown_suffix_is_rejected(self):
        self.assertFalse(self.gate("通过（459/459）；前端测试待验"))

    def test_missing_duplicate_and_stale_rows_are_rejected(self):
        self.assertFalse(self.gate(mutate=lambda rows: rows.pop()))
        self.assertFalse(self.gate(mutate=lambda rows: rows.__setitem__(5, rows[0])))
        self.assertFalse(self.gate(mutate=lambda rows: rows.__setitem__(4, rows[4] + "（重跑）"), stale=True))

    def test_pending_does_not_accept_real_and_vice_versa(self):
        self.assertFalse(self.gate(mode="real"))
        self.assertFalse(self.gate(smoke="有记录（1 次调用）"))


if __name__ == "__main__":
    unittest.main()
