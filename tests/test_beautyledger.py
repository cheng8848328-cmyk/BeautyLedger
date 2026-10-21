"""仅使用虚构数据；所有存储入口在调用前切换到临时路径。"""

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ledger
import main as app
import storage


def sample_records():
    return [
        {"type": "income", "amount_cents": 10000, "note": "模拟收入"},
        {"type": "expense", "amount_cents": 2500, "note": "模拟支出"},
    ]


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="beautyledger-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "data" / "records.json"
        # storage 内部所有文件路径均由 RECORDS_FILE 派生。
        # main 的副本仅用于错误提示，也一并隔离。
        for module in (storage, app):
            patcher = patch.object(module, "RECORDS_FILE", self.path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertEqual(storage.RECORDS_FILE, self.path)
        self.assertEqual(app.RECORDS_FILE, self.path)

    def write_bytes(self, contents):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(contents)


class StorageTests(IsolatedCase):
    def test_missing_file_is_empty_without_creating_file(self):
        self.assertEqual(storage.load_records(), [])
        self.assertFalse(self.path.exists())

    def test_save_load_round_trip_and_empty_ledger(self):
        for records in (sample_records(), []):
            with self.subTest(records=records):
                storage.save_records(records)
                self.assertEqual(storage.load_records(), records)
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), records)
                self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [])

    def test_corrupt_json_and_invalid_utf8_preserve_bytes(self):
        for contents in (b'{"broken":', b'\xff\xfe'):
            with self.subTest(contents=contents):
                self.write_bytes(contents)
                with self.assertRaises(ValueError):
                    storage.load_records()
                self.assertEqual(self.path.read_bytes(), contents)

    def test_invalid_record_formats_rejected_on_load_and_save(self):
        valid = sample_records()[0]
        invalid = [None, {}, "text", [None], [[]], [{}]]
        for field in valid:
            invalid.append([{key: value for key, value in valid.items() if key != field}])
        for record_type in (None, "transfer", 1):
            invalid.append([dict(valid, type=record_type)])
        for amount in (0, -1, True, False, 1.5, "100", None):
            invalid.append([dict(valid, amount_cents=amount)])
        for note in (None, 123, [], {}):
            invalid.append([dict(valid, note=note)])
        for records in invalid:
            with self.subTest(records=records):
                contents = json.dumps(records).encode("utf-8")
                self.write_bytes(contents)
                with self.assertRaises(ValueError):
                    storage.load_records()
                with self.assertRaises(ValueError):
                    storage.save_records(records)
                self.assertEqual(self.path.read_bytes(), contents)
                self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [])

    def test_loading_directory_and_permission_error_are_not_empty_ledger(self):
        self.path.mkdir(parents=True)
        with self.assertRaises(OSError):
            storage.load_records()
        with patch.object(Path, "open", side_effect=PermissionError("模拟拒绝读取")):
            with self.assertRaises(PermissionError):
                storage.load_records()

    def test_save_failure_preserves_original_and_unrelated_temporary_file(self):
        contents = b'[]\n'
        self.write_bytes(contents)
        unrelated = self.path.parent / "records-existing.tmp"
        unrelated.write_bytes(b"keep")
        failure_points = (
            (storage.Path, "mkdir"),
            (storage, "NamedTemporaryFile"),
            (storage.json, "dump"),
            (storage.os, "replace"),
        )
        for target, attribute in failure_points:
            with self.subTest(failure=attribute):
                with patch.object(target, attribute, side_effect=OSError("模拟保存失败")):
                    with self.assertRaisesRegex(OSError, "模拟保存失败"):
                        storage.save_records(sample_records())
                self.assertEqual(self.path.read_bytes(), contents)
                self.assertEqual(unrelated.read_bytes(), b"keep")
                self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [unrelated])

    def test_partial_write_failure_cleans_only_new_temporary_file(self):
        self.write_bytes(b"[]")

        def fail_after_write(records, file, **kwargs):
            file.write('[{"partial":')
            raise OSError("模拟写入中断")

        with patch.object(storage.json, "dump", side_effect=fail_after_write):
            with self.assertRaisesRegex(OSError, "模拟写入中断"):
                storage.save_records(sample_records())
        self.assertEqual(self.path.read_bytes(), b"[]")
        self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [])

    def test_cleanup_failure_does_not_hide_original_save_error(self):
        self.write_bytes(b"[]")
        with patch.object(storage.os, "replace", side_effect=OSError("原始保存错误")):
            with patch.object(Path, "unlink", side_effect=PermissionError("清理失败")):
                with self.assertRaisesRegex(OSError, "原始保存错误"):
                    storage.save_records(sample_records())
        self.assertEqual(self.path.read_bytes(), b"[]")

    def test_save_failure_does_not_create_formal_file(self):
        with patch.object(storage.os, "replace", side_effect=PermissionError("替换失败")):
            with self.assertRaises(PermissionError):
                storage.save_records(sample_records())
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [])

    def test_temporary_file_and_replace_use_isolated_directory(self):
        with patch.object(storage, "NamedTemporaryFile", wraps=storage.NamedTemporaryFile) as create:
            with patch.object(storage.os, "replace", wraps=storage.os.replace) as replace:
                storage.save_records(sample_records())
        self.assertEqual(create.call_args.kwargs["dir"], self.path.parent)
        source, destination = replace.call_args.args
        self.assertEqual(source.parent, self.path.parent)
        self.assertEqual(destination, self.path)


class SummaryTests(unittest.TestCase):
    def test_empty_mixed_negative_balance_and_large_integers(self):
        self.assertEqual(ledger.calculate_summary([]), (0, 0, 0))
        self.assertEqual(ledger.calculate_summary(sample_records()), (10000, 2500, 7500))
        self.assertEqual(ledger.calculate_summary(sample_records()[1:]), (0, 2500, -2500))
        huge = 10 ** 100
        self.assertEqual(ledger.calculate_summary([
            {"type": "income", "amount_cents": huge, "note": "模拟"},
        ]), (huge, 0, huge))

    def test_exact_money_formatting(self):
        for amount, expected in (
            (0, "0.00 SGD"), (1, "0.01 SGD"), (-1, "-0.01 SGD"),
            (101, "1.01 SGD"), (-2500, "-25.00 SGD"),
            (10 ** 100 + 1, "1" + "0" * 98 + ".01 SGD"),
        ):
            with self.subTest(amount=amount):
                self.assertEqual(app.format_sgd(amount), expected)


class MainTests(IsolatedCase):
    def run_app(self, inputs, records=None, load_error=None, save_error=None):
        output = io.StringIO()
        with patch.object(app, "load_records", return_value=records if records is not None else [],
                          side_effect=load_error) as load:
            with patch.object(app, "save_records", side_effect=save_error) as save:
                with patch("builtins.input", side_effect=inputs):
                    with patch("sys.stdout", output):
                        code = app.main()
        return code, output.getvalue(), load, save

    def test_import_does_not_read_save_or_prompt(self):
        spec = importlib.util.spec_from_file_location("beautyledger_import_test", app.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.object(storage, "load_records") as load:
            with patch.object(storage, "save_records") as save:
                with patch("builtins.input") as prompt:
                    spec.loader.exec_module(module)
        load.assert_not_called()
        save.assert_not_called()
        prompt.assert_not_called()

    def test_normal_income_expense_records_and_summary(self):
        code, output, load, save = self.run_app([
            "1", "100", "模拟收入", "2", "25", "模拟支出", "3", "4", "0",
        ])
        self.assertEqual(code, 0)
        load.assert_called_once_with()
        self.assertEqual(save.call_args_list[0].args[0], sample_records()[:1])
        self.assertEqual(save.call_args_list[1].args[0], sample_records())
        self.assertIn("income 100.00 SGD 模拟收入", output)
        self.assertIn("expense 25.00 SGD 模拟支出", output)
        self.assertIn("当前余额：75.00 SGD", output)
        self.assertNotIn("DEBUG", output)

    def test_zero_negative_and_invalid_amounts_do_not_save(self):
        for option in ("1", "2"):
            for amount in ("0", "-1", "abc", "", "1.5"):
                with self.subTest(option=option, amount=amount):
                    code, output, _, save = self.run_app([option, amount, "4", "0"])
                    self.assertEqual(code, 0)
                    save.assert_not_called()
                    self.assertIn("当前余额：0.00 SGD", output)
                    expected = "金额必须大于0" if amount in ("0", "-1") else "请输入正确的整数金额"
                    self.assertIn(expected, output)

    def test_invalid_menu_and_empty_records(self):
        code, output, _, save = self.run_app(["bad", "3", "0"])
        self.assertEqual(code, 0)
        self.assertIn("无效选项", output)
        self.assertIn("暂无收支记录", output)
        save.assert_not_called()

    def test_load_failure_stops_without_saving(self):
        for error in (ValueError("格式错误"), OSError("读取失败")):
            with self.subTest(error=error):
                code, output, _, save = self.run_app([], load_error=error)
                self.assertEqual(code, 1)
                self.assertIn("不会自动覆盖账本", output)
                save.assert_not_called()

    def test_save_failure_does_not_change_memory_and_can_retry(self):
        for error in (OSError("保存失败"), ValueError("保存校验失败")):
            with self.subTest(error=error):
                original = sample_records()[:1]
                code, output, _, save = self.run_app(
                    ["2", "25", "失败模拟", "4", "2", "10", "重试模拟", "4", "0"],
                    records=original, save_error=[error, None],
                )
                self.assertEqual(code, 0)
                self.assertIn("保存失败，本笔账目未录入", output)
                self.assertIn("当前余额：100.00 SGD", output)
                self.assertIn("当前余额：90.00 SGD", output)
                self.assertEqual(output.count("支出录入成功"), 1)
                self.assertEqual(original, sample_records()[:1])
                self.assertEqual(save.call_args.args[0][-1]["note"], "重试模拟")
                self.assertEqual(len(save.call_args.args[0]), 2)

    def test_interrupt_and_eof_at_menu_amount_and_note(self):
        for exception in (KeyboardInterrupt, EOFError):
            for prefix in ([], ["1"], ["1", "10"]):
                with self.subTest(exception=exception, prefix=prefix):
                    code, output, _, save = self.run_app(prefix + [exception()])
                    self.assertEqual(code, 0)
                    self.assertIn("输入已结束", output)
                    save.assert_not_called()


class IntegrationTests(IsolatedCase):
    def invoke(self, inputs):
        output = io.StringIO()
        with patch("builtins.input", side_effect=inputs):
            with patch("sys.stdout", output):
                return app.main(), output

    def test_two_starts_round_trip_through_real_storage_on_temporary_path(self):
        code, _ = self.invoke(["1", "100", "模拟收入", "2", "25", "模拟支出", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(storage.load_records(), sample_records())
        contents = self.path.read_bytes()
        code, output = self.invoke(["3", "4", "0"])
        self.assertEqual(code, 0)
        self.assertIn("模拟收入", output.getvalue())
        self.assertIn("总收入：100.00 SGD", output.getvalue())
        self.assertIn("总支出：25.00 SGD", output.getvalue())
        self.assertIn("当前余额：75.00 SGD", output.getvalue())
        self.assertEqual(self.path.read_bytes(), contents)

    def test_corrupt_and_invalid_ledger_stop_without_overwriting(self):
        for contents in (b"{broken", b'[{}]'):
            with self.subTest(contents=contents):
                self.write_bytes(contents)
                code, output = self.invoke([])
                self.assertEqual(code, 1)
                self.assertIn("无法读取账本", output.getvalue())
                self.assertEqual(self.path.read_bytes(), contents)

    def test_replace_failure_preserves_disk_and_displayed_summary(self):
        storage.save_records(sample_records())
        contents = self.path.read_bytes()
        with patch.object(storage.os, "replace", side_effect=PermissionError("模拟锁定")):
            code, output = self.invoke(["2", "10", "失败模拟", "4", "0"])
        self.assertEqual(code, 0)
        self.assertIn("保存失败", output.getvalue())
        self.assertIn("当前余额：75.00 SGD", output.getvalue())
        self.assertEqual(self.path.read_bytes(), contents)
        self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
