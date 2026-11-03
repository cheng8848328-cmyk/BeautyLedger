"""仅使用虚构数据；所有存储入口在调用前切换到临时路径。"""

import copy
from contextlib import ExitStack
import errno
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

import ledger
import main as app
import storage
import commission_storage as commission_store


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
                # 每个格式在独立的新文件验证，不通过清空已有账本复用路径。
                self.path = self.root / str(len(records)) / "records.json"
                with patch.object(storage, "RECORDS_FILE", self.path):
                    storage.save_records(records)
                    self.assertEqual(storage.load_records(), records)
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), records)
                self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [])

    def test_append_income_expense_and_unchanged_snapshot(self):
        records = sample_records()
        storage.save_records(records[:1])
        storage.save_records(records)
        storage.save_records(records)
        self.assertEqual(storage.load_records(), records)

    def test_history_cannot_be_removed_changed_or_reordered(self):
        records = sample_records()
        storage.save_records(records)
        before = self.path.read_bytes()
        candidates = [[], records[:1], records[1:], list(reversed(records)),
                      [dict(records[0], amount_cents=1), records[1]],
                      [dict(records[0], note="changed"), records[1]]]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    storage.save_records(candidate)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [])

    def test_stale_snapshot_with_new_append_is_rejected(self):
        stale = sample_records()[:1]
        storage.save_records(sample_records())
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            storage.save_records(stale + [dict(stale[0], note="new")])
        self.assertEqual(self.path.read_bytes(), before)

    def test_save_rechecks_corrupt_or_unreadable_existing_file(self):
        for contents in (b"{broken", b"", b"[{}]", b"\xff\xfe"):
            for candidate in ([], sample_records()):
                with self.subTest(contents=contents, candidate=candidate):
                    self.write_bytes(contents)
                    with self.assertRaises(ValueError):
                        storage.save_records(candidate)
                    self.assertEqual(self.path.read_bytes(), contents)
                    self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [])
        self.write_bytes(b"[]")
        with patch.object(Path, "open", side_effect=PermissionError("read denied")) as denied:
            with self.assertRaises(PermissionError):
                storage.save_records(sample_records())
        denied.assert_called_once()
        self.assertEqual(self.path.read_bytes(), b"[]")

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
        with patch.object(Path, "open", side_effect=PermissionError("模拟拒绝读取")) as denied:
            with self.assertRaises(PermissionError):
                storage.load_records()
        denied.assert_called_once()

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
                with patch.object(target, attribute, side_effect=OSError("模拟保存失败")) as fault:
                    with self.assertRaisesRegex(OSError, "模拟保存失败"):
                        storage.save_records(sample_records())
                fault.assert_called_once()
                self.assertEqual(self.path.read_bytes(), contents)
                self.assertEqual(unrelated.read_bytes(), b"keep")
                self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [unrelated])

    def test_partial_write_failure_cleans_only_new_temporary_file(self):
        self.write_bytes(b"[]")

        def fail_after_write(records, file, **kwargs):
            file.write('[{"partial":')
            raise OSError("模拟写入中断")

        with patch.object(storage.json, "dump", side_effect=fail_after_write) as fault:
            with self.assertRaisesRegex(OSError, "模拟写入中断"):
                storage.save_records(sample_records())
        fault.assert_called_once()
        self.assertEqual(self.path.read_bytes(), b"[]")
        self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [])

    def test_cleanup_failure_does_not_hide_original_save_error(self):
        self.write_bytes(b"[]")
        with patch.object(storage.os, "replace", side_effect=OSError("原始保存错误")) as replace:
            with patch.object(Path, "unlink", side_effect=PermissionError("清理失败")) as cleanup:
                with self.assertRaisesRegex(OSError, "原始保存错误"):
                    storage.save_records(sample_records())
        replace.assert_called_once()
        cleanup.assert_called_once()
        self.assertEqual(self.path.read_bytes(), b"[]")

    def test_save_failure_does_not_create_formal_file(self):
        with patch.object(storage.os, "replace", side_effect=PermissionError("替换失败")) as fault:
            with self.assertRaises(PermissionError):
                storage.save_records(sample_records())
        fault.assert_called_once()
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


class CashJsonSafetyTests(IsolatedCase):
    def assert_corrupt_file_preserved(self, contents):
        self.write_bytes(contents)
        with patch.object(storage, "NamedTemporaryFile") as create:
            with patch.object(storage.os, "replace") as replace:
                with self.assertRaises(ValueError):
                    storage.load_records()
                with self.assertRaises(ValueError):
                    storage.save_records(sample_records())
        create.assert_not_called()
        replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(), contents)
        self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [])

    def test_duplicate_keys_rejected_at_all_levels(self):
        cases = (
            b'[{"type":"income","type":"income","amount_cents":100,"note":"DEMO"}]',
            b'[{"type":"income","amount_cents":100,"amount_cents":1,"note":"DEMO"}]',
            b'[{"type":"income","amount_cents":100,"note":"A","note":"B"}]',
            b'[{"type":"income","amount_cents":100,"note":"DEMO","meta":{"a":1,"a":2}}]',
            br'[{"type":"income","amount_cents":100,"note":"DEMO","meta":{"a":1,"\u0061":2}}]',
        )
        for contents in cases:
            with self.subTest(contents=contents):
                self.assert_corrupt_file_preserved(contents)

    def test_nonfinite_json_constants_and_overflow_rejected(self):
        prefix = b'[{"type":"income","amount_cents":100,"note":"DEMO","meta":[{"value":'
        for token in (b"NaN", b"Infinity", b"-Infinity", b"1e400", b"-1e400"):
            with self.subTest(token=token):
                self.assert_corrupt_file_preserved(prefix + token + b"}]}]")

    def test_surrogate_keys_and_nested_values_rejected_on_load(self):
        for surrogate in ("\ud800", "\udfff"):
            for changes in ({"note": surrogate},
                            {"meta": [{"nested": surrogate}]},
                            {"meta": {surrogate: "DEMO"}}):
                with self.subTest(codepoint=ord(surrogate), field=next(iter(changes))):
                    contents = json.dumps([dict(sample_records()[0], **changes)]).encode("ascii")
                    self.assert_corrupt_file_preserved(contents)

    def test_unsafe_python_values_rejected_before_any_file_creation(self):
        class Custom:
            pass

        for value in (("tuple",), {"set"}, Custom(), {1: "key"}, {1: "first", "1": "second"},
                      float("nan"), float("inf"), -float("inf"), "\ud800",
                      {"nested": ["\udfff"]}, {"\ud800": "key"}):
            with self.subTest(value_type=type(value).__name__):
                records = [dict(sample_records()[0], metadata=value)]
                with patch.object(storage.Path, "mkdir") as mkdir:
                    with patch.object(storage, "NamedTemporaryFile") as create:
                        with self.assertRaises(ValueError):
                            storage.validate_records(records)
                        with self.assertRaises(ValueError):
                            storage.save_records(records)
                mkdir.assert_not_called()
                create.assert_not_called()
                self.assertFalse(self.path.parent.exists())

    def test_invalid_append_preserves_original_bytes_and_input(self):
        storage.save_records(sample_records())
        before = self.path.read_bytes()
        for value in (("tuple",), {1: "lost", "1": "kept"}, {"nested": "\ud800"}):
            records = sample_records() + [dict(sample_records()[0], metadata=value)]
            original_input = copy.deepcopy(records)
            with self.subTest(value_type=type(value).__name__):
                with patch.object(storage, "NamedTemporaryFile") as create:
                    with patch.object(storage.os, "replace") as replace:
                        with self.assertRaises(ValueError):
                            storage.save_records(records)
                create.assert_not_called()
                replace.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(records, original_input)

    def test_cycles_rejected_without_recursion_error_or_file_creation(self):
        sequence = []
        sequence.append(sequence)
        mapping = {}
        mapping["self"] = mapping
        for value in (sequence, mapping):
            with self.subTest(value_type=type(value).__name__):
                with patch.object(storage, "NamedTemporaryFile") as create:
                    with self.assertRaises(ValueError):
                        storage.save_records([dict(sample_records()[0], metadata=value)])
                create.assert_not_called()
                self.assertFalse(self.path.parent.exists())

    def test_deep_json_and_python_values_fail_as_value_error(self):
        limit = sys.getrecursionlimit()
        depth = max(5000, limit * 5)
        self.assert_corrupt_file_preserved(
            b'[{"type":"income","amount_cents":100,"note":"DEMO","meta":'
            + b"[" * depth + b"0" + b"]" * depth + b"}]")
        nested = 0
        for _ in range(depth):
            nested = [nested]
        with patch.object(storage, "NamedTemporaryFile") as create:
            with self.assertRaises(ValueError):
                storage.save_records([dict(sample_records()[0], metadata=nested)])
        create.assert_not_called()
        self.assertEqual(sys.getrecursionlimit(), limit)

    def test_valid_unicode_big_integers_and_metadata_round_trip(self):
        records = [dict(type="income", amount_cents=10 ** 1000 + 1, note="中文 \U0001f9f4",
                        metadata={"中文键": [True, False, None, 1, 1.0, 1.25, -0.0, {"x": "😀"}]})]
        before = copy.deepcopy(records)
        storage.save_records(records)
        self.assertEqual(storage.load_records(), before)
        self.assertEqual(records, before)
        # A proper escaped surrogate pair represents one valid Unicode character.
        escaped = json.dumps([dict(type="income", amount_cents=1, note="😀")]).encode("ascii")
        self.write_bytes(escaped)
        loaded = storage.load_records()
        self.assertEqual(loaded[0]["note"], "😀")
        storage.save_records(loaded)
        self.assertEqual(storage.load_records(), loaded)

    def test_shared_noncyclic_containers_are_accepted(self):
        shared = {"notes": ["DEMO"]}
        records = [dict(sample_records()[0], metadata=[shared, shared])]
        storage.save_records(records)
        self.assertEqual(storage.load_records(), records)
        self.assertIs(records[0]["metadata"][0], records[0]["metadata"][1])

    def test_nested_metadata_within_runtime_capacity_is_not_arbitrarily_limited(self):
        nested = "DEMO"
        for _ in range(128):
            nested = [nested]
        records = [dict(sample_records()[0], metadata=nested)]
        storage.save_records(records)
        self.assertEqual(storage.load_records(), records)

    def test_history_comparison_preserves_nested_types(self):
        for old, changed in ((True, 1), (1, 1.0), (False, 0), (0, False)):
            with self.subTest(old_type=type(old).__name__, new_type=type(changed).__name__):
                original = [dict(sample_records()[0], metadata={"items": [{"flag": old}]})]
                self.write_bytes(json.dumps(original).encode("utf-8"))
                before = self.path.read_bytes()
                candidate = [dict(original[0], metadata={"items": [{"flag": changed}]})]
                with self.assertRaises(ValueError):
                    storage.save_records(candidate)
                self.assertEqual(self.path.read_bytes(), before)

    def test_history_object_order_ignored_but_fields_and_array_order_protected(self):
        original = [dict(sample_records()[0], metadata={"a": 1, "b": [1, 2]})]
        storage.save_records(original)
        reordered = [dict(reversed(list(original[0].items())))]
        reordered[0]["metadata"] = {"b": [1, 2], "a": 1}
        storage.save_records(reordered)
        before = self.path.read_bytes()
        for metadata in ({"a": 1}, {"a": 1, "b": [1, 2], "c": None}, {"a": 1, "b": [2, 1]}):
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValueError):
                    storage.save_records([dict(original[0], metadata=metadata)])
                self.assertEqual(self.path.read_bytes(), before)

    def test_disk_full_partial_write_preserves_bytes_and_unrelated_temp(self):
        storage.save_records(sample_records())
        before = self.path.read_bytes()
        unrelated = self.path.parent / "records-unrelated.tmp"
        unrelated.write_bytes(b"keep")

        def disk_full(records, stream, **kwargs):
            stream.write('[{"partial":')
            raise OSError(errno.ENOSPC, "simulated disk full")

        with patch.object(storage.json, "dump", side_effect=disk_full) as fault:
            with self.assertRaises(OSError) as error:
                storage.save_records(sample_records() + [dict(sample_records()[0], note="NEXT")])
        fault.assert_called_once()
        self.assertEqual(error.exception.errno, errno.ENOSPC)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(unrelated.read_bytes(), b"keep")
        self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [unrelated])

    def test_recursion_during_partial_write_is_normalized_and_cleans_temp(self):
        storage.save_records(sample_records())
        before = self.path.read_bytes()

        def interrupted_dump(records, stream, **kwargs):
            stream.write("[")
            raise RecursionError("simulated encoding recursion")

        with patch.object(storage.json, "dump", side_effect=interrupted_dump) as fault:
            with self.assertRaises(ValueError):
                storage.save_records(sample_records())
        fault.assert_called_once()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [])


class CashFinalRegressionTests(IsolatedCase):
    def test_recursion_in_each_history_encoding_preserves_file_before_io(self):
        history = sample_records()
        storage.save_records(history)
        before = self.path.read_bytes()
        candidate = history + [dict(history[0], note="DEMO-NEXT")]
        original_input = copy.deepcopy(candidate)
        real_dumps = json.dumps
        for failing_call in (1, 2):
            with self.subTest(history_encoding=failing_call):
                fault = RecursionError("history encoding fault")
                history_calls = []

                def encode(value, **kwargs):
                    # Preflight encoding must succeed; fail only sort_keys encoding.
                    if kwargs.get("sort_keys"):
                        history_calls.append(copy.deepcopy(value))
                        if len(history_calls) == failing_call:
                            raise fault
                    return real_dumps(value, **kwargs)

                with patch.object(json, "dumps", side_effect=encode), \
                        patch.object(storage, "load_records", wraps=storage.load_records) as load, \
                        patch.object(storage.Path, "mkdir") as mkdir, \
                        patch.object(storage, "NamedTemporaryFile") as create, \
                        patch.object(storage.os, "replace") as replace:
                    with self.assertRaises(ValueError) as error:
                        storage.save_records(candidate)
                self.assertIs(error.exception.__cause__, fault)
                self.assertEqual(history_calls, [history] * failing_call)
                load.assert_called_once_with()
                mkdir.assert_not_called()
                create.assert_not_called()
                replace.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(candidate, original_input)
                self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [])

    def assert_stream_failure(self, stage, *, cleanup_failure=False):
        history = sample_records()
        storage.save_records(history)
        before = self.path.read_bytes()
        candidate = history + [dict(history[0], note="DEMO-NEXT")]
        original_input = copy.deepcopy(candidate)
        unrelated = self.path.parent / "records-unrelated.tmp"
        unrelated.write_bytes(b"keep")
        fault = KeyboardInterrupt("write interrupted") if stage == "write" else OSError(stage + " failed")
        real_temporary = storage.NamedTemporaryFile
        created = []
        streams = []
        injected = []
        positions = []
        with ExitStack() as patches:
            def create_file(**kwargs):
                stream = real_temporary(**kwargs)
                raw = stream.file
                created.append(Path(stream.name))
                streams.append(raw)
                real_operation = getattr(raw, stage)
                calls = 0

                def fail_operation(*args, **options):
                    nonlocal calls
                    calls += 1
                    # Allow a later close-time flush to release the real handle.
                    if calls > 1:
                        return real_operation(*args, **options)
                    if stage == "write":
                        real_operation(*args, **options)
                    positions.append(raw.tell())
                    if stage == "close":
                        real_operation(*args, **options)
                    raise fault

                injected.append(patches.enter_context(
                    patch.object(raw, stage, side_effect=fail_operation)))
                return stream

            create = patches.enter_context(patch.object(storage, "NamedTemporaryFile", side_effect=create_file))
            dump = patches.enter_context(patch.object(storage.json, "dump", wraps=storage.json.dump))
            replace = patches.enter_context(patch.object(storage.os, "replace"))
            if cleanup_failure:
                cleanup = patches.enter_context(patch.object(
                    Path, "unlink", autospec=True, side_effect=PermissionError("cleanup denied")))
            with self.assertRaises(type(fault)) as error:
                storage.save_records(candidate)

        self.assertIs(error.exception, fault)
        create.assert_called_once()
        dump.assert_called_once()
        replace.assert_not_called()
        self.assertEqual(len(injected), 1)
        injected[0].assert_called()
        self.assertEqual(len(positions), 1)
        self.assertGreater(positions[0], 0)
        self.assertTrue(streams[0].closed)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(candidate, original_input)
        self.assertEqual(unrelated.read_bytes(), b"keep")
        if cleanup_failure:
            cleanup.assert_called_once_with(created[0])
            self.assertEqual(set(self.path.parent.glob("records-*.tmp")), {unrelated, created[0]})
            created[0].unlink()  # Remove only the deliberately stranded test file.
        self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [unrelated])

    def test_implicit_flush_failure_preserves_original_and_cleans_temp(self):
        self.assert_stream_failure("flush")

    def test_close_failure_preserves_original_and_cleans_temp(self):
        self.assert_stream_failure("close")

    def test_keyboard_interrupt_during_write_propagates_and_cleans_temp(self):
        self.assert_stream_failure("write")

    def test_cleanup_failure_preserves_original_io_error_or_control_signal(self):
        for stage in ("flush", "close", "write"):
            with self.subTest(stage=stage):
                self.assert_stream_failure(stage, cleanup_failure=True)

    def test_nested_builtin_subclasses_are_rejected_before_temporary_creation(self):
        history = sample_records()
        storage.save_records(history)
        before = self.path.read_bytes()
        for builtin, value in ((int, 1), (float, 1.0), (str, "DEMO"), (list, [1]), (dict, {"x": 1})):
            with self.subTest(builtin=builtin.__name__):
                subclass = type("Custom" + builtin.__name__, (builtin,), {})
                custom = subclass(value)
                normal = history + [dict(history[0], metadata={"nested": [value]})]
                storage.validate_records(normal)
                candidate = history + [dict(history[0], metadata={"nested": [custom]})]
                with patch.object(storage, "NamedTemporaryFile") as create:
                    with self.assertRaises(ValueError):
                        storage.save_records(candidate)
                create.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)
                self.assertIs(candidate[-1]["metadata"]["nested"][0], custom)

    def test_integer_conversion_limit_rejects_memory_and_disk_without_overwrite(self):
        history = sample_records()
        storage.save_records(history)
        before = self.path.read_bytes()
        previous = sys.get_int_max_str_digits()
        self.addCleanup(sys.set_int_max_str_digits, previous)
        limit = sys.int_info.str_digits_check_threshold
        sys.set_int_max_str_digits(limit)
        candidate = history + [dict(history[0], metadata=10 ** limit)]
        with patch.object(storage, "NamedTemporaryFile") as create:
            with self.assertRaises(ValueError):
                storage.save_records(candidate)
        create.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        disk = json.dumps([dict(history[0], metadata="HUGE")]).encode("utf-8")
        disk = disk.replace(b'"HUGE"', b"1" + b"0" * limit)
        self.path.write_bytes(disk)
        with patch.object(storage, "NamedTemporaryFile") as create:
            with self.assertRaises(ValueError):
                storage.load_records()
            with self.assertRaises(ValueError):
                storage.save_records(history)
        create.assert_not_called()
        self.assertEqual(self.path.read_bytes(), disk)


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
                code, output, load, save = self.run_app([], load_error=error)
                self.assertEqual(code, 1)
                self.assertIn("不会自动覆盖账本", output)
                load.assert_called_once()
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
    def test_corruption_after_start_blocks_save_without_success_or_memory_change(self):
        storage.save_records(sample_records())
        inputs = iter(["1", "10", "DEMO", "4", "0"])
        output = io.StringIO()

        def respond(prompt):
            value = next(inputs)
            if value == "DEMO":
                self.path.write_bytes(b"{broken during input")
            return value

        with patch("builtins.input", side_effect=respond), patch("sys.stdout", output):
            self.assertEqual(app.main(), 0)
        self.assertIn("保存失败", output.getvalue())
        self.assertNotIn("收入录入成功", output.getvalue())
        self.assertIn("当前余额：75.00 SGD", output.getvalue())
        self.assertEqual(self.path.read_bytes(), b"{broken during input")

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
        with patch.object(storage.os, "replace", side_effect=PermissionError("模拟锁定")) as fault:
            code, output = self.invoke(["2", "10", "失败模拟", "4", "0"])
        fault.assert_called_once()
        self.assertEqual(code, 0)
        self.assertIn("保存失败", output.getvalue())
        self.assertIn("当前余额：75.00 SGD", output.getvalue())
        self.assertEqual(self.path.read_bytes(), contents)
        self.assertEqual(list(self.path.parent.glob("records-*.tmp")), [])


class CommissionCliTests(IsolatedCase):
    def test_disk_append_during_input_rejects_stale_save_without_success(self):
        original = dict(entry_id="OLD", employee_id="DEMO", month="2026-10",
                        commission_cents=100)
        extra = dict(original, entry_id="EXTRA", transaction_id="EXTRA-TX",
                     audit_version=1, entry_type="service", original_price_cents=1000,
                     rate_bps=1000, completed=True, participant_count=1)
        self.seed_legacy_entries([original])
        inputs = iter(["6", "DEMO", "2026-10", "200", "0"])
        output = io.StringIO()

        def respond(prompt):
            value = next(inputs)
            if value == "200":
                commission_store.save_commission_records([original, extra], self.commission_path)
            return value

        with patch("builtins.input", side_effect=respond), patch("sys.stdout", output):
            self.assertEqual(app.main(), 0)
        self.assertIn("提成保存失败", output.getvalue())
        self.assertNotIn("提成录入成功", output.getvalue())
        self.assertEqual(self.read_entries(), [original, extra])

    def setUp(self):
        super().setUp()
        self.commission_path = self.root / "data" / "commissions.json"
        patcher = patch.object(app, "COMMISSIONS_FILE", self.commission_path, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def invoke(self, inputs):
        output = io.StringIO()
        with patch("builtins.input", side_effect=inputs):
            with patch("sys.stdout", output):
                code = app.main()
        self.assertEqual(code, 0)
        return output.getvalue()

    def read_entries(self):
        return commission_store.load_commission_records(self.commission_path)

    def seed_legacy_entries(self, entries):
        # 模拟升级前已经存在的文件；不通过新版本保存接口制造旧格式新增记录。
        self.commission_path.parent.mkdir(parents=True, exist_ok=True)
        self.commission_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    def seed_reversal_service(self):
        self.invoke(["5", "DEMO-ALICE", "2026-10", "60", "30", "y", "0"])
        return self.read_entries()[0]

    def test_reversal_same_month_keeps_original_and_reference(self):
        original = self.seed_reversal_service()
        output = self.invoke(["9", original["entry_id"], "2026-10", "YES",
                              "8", "2026-10", "0"])
        self.assertIn("提成冲回记录已保存", output)
        self.assertIn("DEMO-ALICE：0.00 SGD", output)
        entries = self.read_entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0], original)
        reversal = entries[1]
        self.assertEqual(reversal["reversal_of_entry_id"], original["entry_id"])
        self.assertEqual(reversal["commission_cents"], -1800)
        self.assertEqual(reversal["employee_id"], "DEMO-ALICE")
        self.assertNotEqual(reversal["entry_id"], original["entry_id"])
        self.assertNotIn("audit_version", reversal)
        self.assertNotIn("original_price_cents", reversal)
        self.assertFalse(self.path.exists())

    def test_reversal_cross_month_query_after_restart(self):
        original = self.seed_reversal_service()
        self.invoke(["9", original["entry_id"], "2026-11", "YES", "0"])
        output = self.invoke(["8", "2026-10", "8", "2026-11", "0"])
        self.assertIn("DEMO-ALICE：18.00 SGD", output)
        self.assertIn("DEMO-ALICE：-18.00 SGD", output)
        self.assertEqual(self.read_entries()[1]["month"], "2026-11")

    def test_reversal_preview_shows_basis_and_scope(self):
        original = self.seed_reversal_service()
        before = self.commission_path.read_bytes()
        output = self.invoke(["9", original["entry_id"], "2026-11", "NO", "0"])
        for text in ("DEMO-ALICE", "18.00 SGD", "-18.00 SGD", "2026-10", "2026-11",
                     original["transaction_id"], "service", "60.00 SGD", "3000", "True",
                     "不执行实际退款", "不自动扣回已发放工资", "已取消"):
            self.assertIn(text, output)
        self.assertEqual(self.commission_path.read_bytes(), before)

    def test_reversal_requires_exact_yes(self):
        original = self.seed_reversal_service()
        before = self.commission_path.read_bytes()
        for confirmation in ("", "yes", "y", "YES ", " YES", "NO", "q"):
            with self.subTest(confirmation=confirmation):
                output = self.invoke(["9", original["entry_id"], "2026-10", confirmation, "0"])
                self.assertIn("已取消", output)
                self.assertNotIn("提成冲回记录已保存", output)
                self.assertEqual(self.commission_path.read_bytes(), before)

    def test_reversal_cancel_invalid_month_and_interrupt(self):
        original = self.seed_reversal_service()
        before = self.commission_path.read_bytes()
        for inputs in (["9", "q", "0"], ["9", original["entry_id"], "q", "0"],
                       ["9", original["entry_id"], "2026-13", "0"]):
            self.invoke(inputs)
            self.assertEqual(self.commission_path.read_bytes(), before)
        for error in (EOFError, KeyboardInterrupt):
            for prefix in (["9"], ["9", original["entry_id"]],
                           ["9", original["entry_id"], "2026-10"]):
                with self.subTest(error=error, prefix=prefix):
                    self.invoke(prefix + [error()])
                    self.assertEqual(self.commission_path.read_bytes(), before)

    def test_reversal_rejects_missing_zero_negative_and_duplicate(self):
        original = self.seed_reversal_service()
        self.invoke(["5", "ZERO", "2026-10", "60", "30", "n", "0"])
        zero = self.read_entries()[1]
        self.invoke(["9", original["entry_id"], "2026-10", "YES", "0"])
        negative = self.read_entries()[2]
        before = self.commission_path.read_bytes()
        for entry_id, expected in (("missing", "不存在"), (zero["entry_id"], "正向提成"),
                                   (negative["entry_id"], "正向提成"),
                                   (original["entry_id"], "已有冲回")):
            with self.subTest(entry_id=entry_id):
                output = self.invoke(["9", entry_id, "0"])
                self.assertIn(expected, output)
                self.assertNotIn("提成冲回记录已保存", output)
                self.assertEqual(self.commission_path.read_bytes(), before)

    def test_reversal_shared_service_only_selected_employee(self):
        self.invoke(["7", "DEMO-ALICE", "DEMO-BOB", "2026-10", "100", "30", "y", "0"])
        alice, bob = self.read_entries()
        output = self.invoke(["9", alice["entry_id"], "2026-10", "YES", "8", "2026-10", "0"])
        self.assertIn("仅冲回当前员工的一条明细", output)
        self.assertIn("检查关联的其他员工明细", output)
        self.assertIn(bob["entry_id"], output)
        self.assertIn("DEMO-ALICE：0.00 SGD", output)
        self.assertIn("DEMO-BOB：15.00 SGD", output)
        self.assertEqual(self.read_entries()[:2], [alice, bob])
        self.invoke(["9", bob["entry_id"], "2026-10", "YES", "0"])
        self.assertEqual(len(self.read_entries()), 4)

    def test_reversal_legacy_shared_records_warn_and_remain_unchanged(self):
        entries = [dict(entry_id="old-a", employee_id="A", month="2026-10",
                        commission_cents=1500, transaction_id="old-tx", entry_type="service"),
                   dict(entry_id="old-b", employee_id="B", month="2026-10",
                        commission_cents=1500, transaction_id="old-tx", entry_type="service")]
        self.seed_legacy_entries(entries)
        output = self.invoke(["9", "old-a", "2026-10", "YES", "0"])
        self.assertIn("仅冲回当前员工的一条明细", output)
        self.assertEqual(self.read_entries()[:2], entries)

    def test_reversal_topup_and_minimal_legacy_supported(self):
        legacy = dict(entry_id="legacy", employee_id="OLD", month="2026-09", commission_cents=500)
        self.seed_legacy_entries([legacy])
        self.invoke(["6", "DEMO-ALICE", "2026-10", "200", "0"])
        topup = self.read_entries()[1]
        output = self.invoke(["9", topup["entry_id"], "2026-10", "YES",
                              "9", "legacy", "2026-11", "YES", "0"])
        self.assertIn("充值金额：200.00 SGD", output)
        self.assertIn("未记录", output)
        self.assertEqual([e["commission_cents"] for e in self.read_entries()], [500, 1000, -1000, -500])

    def test_reversal_save_failure_preserves_old_file(self):
        original = self.seed_reversal_service()
        before = self.commission_path.read_bytes()
        with patch.object(commission_store.os, "replace", side_effect=PermissionError("模拟锁定")) as fault:
            output = self.invoke(["9", original["entry_id"], "2026-10", "YES", "0"])
        fault.assert_called_once()
        self.assertIn("提成冲回保存失败", output)
        self.assertNotIn("提成冲回记录已保存", output)
        self.assertEqual(self.commission_path.read_bytes(), before)
        self.assertEqual(list(self.commission_path.parent.glob("commissions-*.tmp")), [])

    def test_reversal_corrupt_json_not_overwritten(self):
        self.commission_path.parent.mkdir(parents=True)
        self.commission_path.write_bytes(b"{broken")
        output = self.invoke(["9", "0"])
        self.assertIn("无法读取提成记录", output)
        self.assertEqual(self.commission_path.read_bytes(), b"{broken")

    def test_reversal_refresh_rejects_new_duplicate(self):
        original = self.seed_reversal_service()
        latest = [original, dict(entry_id="external-r", employee_id="DEMO-ALICE", month="2026-11",
                                 commission_cents=-1800, reversal_of_entry_id=original["entry_id"])]
        with patch.object(commission_store, "load_commission_records", side_effect=[[original], latest]):
            with patch.object(commission_store, "save_commission_records") as save:
                output = self.invoke(["9", original["entry_id"], "2026-11", "YES", "0"])
        self.assertIn("已有冲回", output)
        save.assert_not_called()

    def test_reversal_refresh_rejects_changed_preview_or_missing_original(self):
        original = self.seed_reversal_service()
        changed = dict(original, original_price_cents=9000, rate_bps=2000)
        for latest in ([], [changed]):
            with self.subTest(latest=latest):
                with patch.object(commission_store, "load_commission_records", side_effect=[[original], latest]):
                    with patch.object(commission_store, "save_commission_records") as save:
                        output = self.invoke(["9", original["entry_id"], "2026-11", "YES", "0"])
                self.assertIn("原明细已变化或不存在", output)
                save.assert_not_called()

    def test_reversal_refresh_keeps_unrelated_new_entries(self):
        original = self.seed_reversal_service()
        extra = dict(entry_id="extra", employee_id="OTHER", month="2026-10", commission_cents=100)
        with patch.object(commission_store, "load_commission_records",
                          side_effect=[[original], [original, extra]]):
            with patch.object(commission_store, "save_commission_records") as save:
                self.invoke(["9", original["entry_id"], "2026-11", "YES", "0"])
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0][:2], [original, extra])
        self.assertEqual(len(save.call_args.args[0]), 3)

    def test_audit_service_inputs_survive_reload(self):
        self.invoke(["5", "Alice", "2026-10", "60.50", "20", "y",
                     "5", "Bob", "2026-10", "100", "12.5", "n", "0"])
        first, second = self.read_entries()
        for entry, price, rate, completed, amount in (
            (first, 6050, 2000, True, 1210), (second, 10000, 1250, False, 0),
        ):
            self.assertEqual(entry["audit_version"], 1)
            self.assertEqual(entry["participant_count"], 1)
            self.assertEqual(entry["original_price_cents"], price)
            self.assertEqual(entry["rate_bps"], rate)
            self.assertIs(entry["completed"], completed)
            self.assertEqual(entry["commission_cents"], amount)

    def test_audit_topup_input_survives_reload(self):
        self.invoke(["6", "Alice", "2026-10", "200.00", "0"])
        entry = self.read_entries()[0]
        self.assertEqual(entry["audit_version"], 1)
        self.assertEqual(entry["topup_amount_cents"], 20000)
        self.assertEqual(entry["commission_cents"], 1000)
        self.assertNotIn("original_price_cents", entry)

    def test_audit_shared_service_preserves_common_basis(self):
        self.invoke(["7", "Alice", "Bob", "2026-10", "100", "30", "y", "0"])
        entries = self.read_entries()
        self.assertEqual(len({entry["transaction_id"] for entry in entries}), 1)
        for entry in entries:
            self.assertEqual(entry["participant_count"], 2)
            self.assertEqual(entry["original_price_cents"], 10000)
            self.assertEqual(entry["rate_bps"], 3000)
            self.assertIs(entry["completed"], True)
            self.assertEqual(entry["commission_cents"], 1500)

    def test_audit_legacy_preserved_on_append_and_save_failure(self):
        legacy = [{"entry_id": "old", "employee_id": "Alice", "month": "2026-09",
                   "commission_cents": 123, "entry_type": "service", "transaction_id": "old-tx"}]
        self.seed_legacy_entries(legacy)
        before = self.commission_path.read_bytes()
        with patch.object(commission_store.os, "replace", side_effect=OSError("模拟失败")) as fault:
            output = self.invoke(["6", "Bob", "2026-10", "200", "0"])
        fault.assert_called_once()
        self.assertNotIn("提成录入成功", output)
        self.assertEqual(self.commission_path.read_bytes(), before)
        self.invoke(["6", "Bob", "2026-10", "200", "0"])
        self.assertEqual(self.read_entries()[0], legacy[0])

    def test_single_service_saved_and_reloaded(self):
        output = self.invoke(["5", "Alice", "2026-10", "60.00", "30", "y", "0"])
        self.assertIn("提成录入成功", output)
        entries = self.read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["employee_id"], "Alice")
        self.assertEqual(entries[0]["month"], "2026-10")
        self.assertEqual(entries[0]["commission_cents"], 1800)
        self.assertEqual(entries[0]["entry_type"], "service")
        self.assertTrue(entries[0]["entry_id"])
        self.assertFalse(self.path.exists())

    def test_topup_saved_with_fixed_rate(self):
        self.invoke(["6", "Alice", "2026-10", "1000", "0"])
        entry = self.read_entries()[0]
        self.assertEqual(entry["commission_cents"], 5000)
        self.assertEqual(entry["entry_type"], "topup")
        self.assertFalse(self.path.exists())

    def test_shared_service_one_calculation_one_save(self):
        with patch.object(commission_store, "save_commission_records",
                          wraps=commission_store.save_commission_records) as save:
            with patch.object(app, "calculate_service_commission",
                              wraps=app.calculate_service_commission) as calculate:
                self.invoke(["7", "Alice", "Bob", "2026-10", "100", "30", "y", "0"])
        save.assert_called_once()
        calculate.assert_called_once_with(10000, 3000, True)
        self.assertEqual(len(save.call_args.args[0]), 2)
        entries = self.read_entries()
        self.assertEqual([e["commission_cents"] for e in entries], [1500, 1500])
        self.assertEqual(len({e["entry_id"] for e in entries}), 2)
        self.assertEqual(len({e["transaction_id"] for e in entries}), 1)
        self.assertTrue(entries[0]["transaction_id"])
        self.assertTrue(all(e["entry_type"] == "service" for e in entries))

    def test_month_query_after_restart_and_repeated_employee(self):
        self.invoke(["5", "Alice", "2026-10", "60", "30", "y",
                     "6", "Alice", "2026-10", "1000",
                     "7", "Alice", "Bob", "2026-10", "100", "30", "y",
                     "6", "Alice", "2026-11", "200", "0"])
        entries = self.read_entries()
        self.assertEqual(len({e["entry_id"] for e in entries}), 5)
        self.assertEqual(len({e["transaction_id"] for e in entries}), 4)
        before = self.commission_path.read_bytes()
        output = self.invoke(["8", "2026-10", "0"])
        self.assertIn("Alice：83.00 SGD", output)
        self.assertIn("Bob：15.00 SGD", output)
        output = self.invoke(["8", "2026-11", "0"])
        self.assertIn("Alice：10.00 SGD", output)
        self.assertNotIn("Bob：", output)
        self.assertEqual(self.commission_path.read_bytes(), before)

    def test_empty_query_does_not_create_file(self):
        output = self.invoke(["8", "2026-10", "0"])
        self.assertIn("该月暂无提成记录", output)
        self.assertFalse(self.commission_path.exists())

    def test_strict_amount_parser_and_decimal_precision(self):
        for value, expected in (("60", 6000), ("60.00", 6000), ("60.50", 6050)):
            with self.subTest(value=value):
                self.assertEqual(app.parse_commission_amount(value), expected)
        for value in ("", "0", "-1", "+1", "1.5", "1.001", "1e2", "NaN",
                      "1_000", "1,000", ".50", "６０", " 60", "60\n"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    app.parse_commission_amount(value)
        self.invoke(["5", "Alice", "2026-10", "60.50", "20", "y", "0"])
        self.assertEqual(self.read_entries()[0]["commission_cents"], 1210)

    def test_invalid_inputs_return_to_menu_without_saving(self):
        cases = (["5", " "], ["6", "Alice", "2026-13"],
                 ["6", "Alice", "2026-10", "1e2"],
                 ["5", "Alice", "2026-10", "60", ""],
                 ["5", "Alice", "2026-10", "60", "100.01"],
                 ["5", "Alice", "2026-10", "60", "-1"],
                 ["5", "Alice", "2026-10", "60", "30", "maybe"],
                 ["7", "Alice", "Alice"], ["8", "2026-1"])
        for inputs in cases:
            with self.subTest(inputs=inputs):
                output = self.invoke(inputs + ["4", "0"])
                self.assertIn("输入无效", output)
                self.assertIn("当前余额：0.00 SGD", output)
                self.assertNotIn("提成录入成功", output)
                self.assertFalse(self.commission_path.exists())

    def test_no_rounding_for_fractional_or_odd_cents(self):
        for inputs in (["6", "Alice", "2026-10", "0.01"],
                       ["5", "Alice", "2026-10", "0.01", "30", "y"],
                       ["7", "Alice", "Bob", "2026-10", "0.10", "30", "y"]):
            with self.subTest(inputs=inputs):
                output = self.invoke(inputs + ["0"])
                self.assertIn("舍入规则尚未确认", output)
                self.assertNotIn("提成录入成功", output)
                self.assertFalse(self.commission_path.exists())

    def test_explicit_fractional_percent_and_uncompleted_service(self):
        self.invoke(["5", "Alice", "2026-10", "100", "12.5", "y",
                     "5", "Bob", "2026-10", "100", "30", "n", "0"])
        self.assertEqual([e["commission_cents"] for e in self.read_entries()], [1250, 0])

    def test_cancel_at_any_prompt(self):
        flow = ["7", "Alice", "Bob", "2026-10", "100", "30", "y"]
        for count in range(1, len(flow)):
            with self.subTest(count=count):
                output = self.invoke(flow[:count] + ["q", "0"])
                self.assertIn("已取消", output)
                self.assertFalse(self.commission_path.exists())

    def test_interruption_does_not_save_partial_entries(self):
        for error in (EOFError, KeyboardInterrupt):
            output = self.invoke(["7", "Alice", "Bob", "2026-10", error()])
            self.assertIn("输入已结束", output)
            self.assertFalse(self.commission_path.exists())

    def test_shared_save_failure_preserves_file_and_allows_retry(self):
        self.invoke(["6", "Alice", "2026-10", "200", "0"])
        before = self.commission_path.read_bytes()
        with patch.object(commission_store.os, "replace", side_effect=OSError("模拟锁定")) as fault:
            output = self.invoke(["7", "Alice", "Bob", "2026-10", "100", "30", "y",
                                  "8", "2026-10", "0"])
        fault.assert_called_once()
        self.assertIn("提成保存失败", output)
        self.assertNotIn("提成录入成功", output)
        self.assertIn("Alice：10.00 SGD", output)
        self.assertNotIn("Bob：", output)
        self.assertEqual(self.commission_path.read_bytes(), before)
        self.invoke(["7", "Alice", "Bob", "2026-10", "100", "30", "y", "0"])
        self.assertEqual(len(self.read_entries()), 3)

    def test_load_corruption_blocks_commissions_but_not_old_menu(self):
        self.commission_path.parent.mkdir(parents=True)
        self.commission_path.write_bytes(b"{broken")
        output = self.invoke(["5", "8", "2026-10", "4", "0"])
        self.assertIn("无法读取提成记录", output)
        self.assertIn("当前余额：0.00 SGD", output)
        self.assertNotIn("提成录入成功", output)
        self.assertEqual(self.commission_path.read_bytes(), b"{broken")

    def test_each_entry_loads_latest_snapshot(self):
        with patch.object(commission_store, "load_commission_records",
                          wraps=commission_store.load_commission_records) as load:
            self.invoke(["6", "Alice", "2026-10", "200",
                         "6", "Bob", "2026-10", "200", "0"])
        # 每次录入加载一次，现有保存函数还会独立读取并验证旧文件。
        self.assertEqual(load.call_count, 4)
        self.assertTrue(all(call.args == (self.commission_path,) for call in load.call_args_list))
        self.assertEqual(len(self.read_entries()), 2)

    def test_ledger_and_commissions_remain_separate(self):
        storage.save_records(sample_records())
        before = self.path.read_bytes()
        output = self.invoke(["6", "Alice", "2026-10", "200", "3", "4", "0"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("income 100.00 SGD 模拟收入", output)
        self.assertIn("当前余额：75.00 SGD", output)
        self.assertIn("不确认收款或会员余额变化", output)


class HardenedCliTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.commission_path = self.root / "data" / "commissions.json"
        patcher = patch.object(app, "COMMISSIONS_FILE", self.commission_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def invoke(self, inputs):
        output = io.StringIO()
        with patch("builtins.input", side_effect=inputs), patch("sys.stdout", output):
            code = app.main()
        return code, output.getvalue()

    def test_strict_cash_errors_stop_startup_without_success(self):
        cases = (
            b'[{"type":"income","amount_cents":100,"amount_cents":1,"note":"DEMO"}]',
            b'[{"type":"income","amount_cents":100,"note":"DEMO","meta":NaN}]',
            br'[{"type":"income","amount_cents":100,"note":"\ud800"}]',
            b"[" * max(5000, sys.getrecursionlimit() * 5) + b"0"
            + b"]" * max(5000, sys.getrecursionlimit() * 5),
        )
        for contents in cases:
            with self.subTest(size=len(contents)):
                self.write_bytes(contents)
                with patch.object(storage, "NamedTemporaryFile") as create:
                    code, output = self.invoke([])
                self.assertEqual(code, 1)
                self.assertIn("无法读取账本", output)
                self.assertNotIn("录入成功", output)
                self.assertNotIn("Traceback", output)
                self.assertNotIn("RecursionError", output)
                create.assert_not_called()
                self.assertEqual(self.path.read_bytes(), contents)

    def test_invalid_note_cannot_be_saved_or_change_displayed_balance(self):
        storage.save_records(sample_records())
        before = self.path.read_bytes()
        with patch.object(storage, "NamedTemporaryFile") as create:
            code, output = self.invoke(["1", "10", "\ud800", "4", "0"])
        self.assertEqual(code, 0)
        self.assertIn("保存失败", output)
        self.assertNotIn("收入录入成功", output)
        self.assertIn("当前余额：75.00 SGD", output)
        create.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_strict_commission_errors_block_all_operations_without_writing(self):
        prefix = b'[{"entry_id":"OLD","employee_id":"DEMO","month":"2026-10","commission_cents":100'
        cases = (
            prefix + b',"entry_id":"SECOND"}]',
            prefix + b',"meta":1e400}]',
            prefix + br',"meta":{"text":"\udfff"}}]',
            b"[" * max(5000, sys.getrecursionlimit() * 5) + b"0"
            + b"]" * max(5000, sys.getrecursionlimit() * 5),
        )
        self.commission_path.parent.mkdir(parents=True, exist_ok=True)
        for contents in cases:
            with self.subTest(size=len(contents)):
                self.commission_path.write_bytes(contents)
                with patch.object(commission_store, "NamedTemporaryFile") as create:
                    code, output = self.invoke(["5", "6", "7", "8", "2026-10", "9", "4", "0"])
                self.assertEqual(code, 0)
                self.assertEqual(output.count("无法读取提成记录"), 5)
                self.assertNotIn("提成录入成功", output)
                self.assertNotIn("提成冲回记录已保存", output)
                self.assertNotIn("Traceback", output)
                self.assertNotIn("RecursionError", output)
                self.assertIn("当前余额：0.00 SGD", output)
                create.assert_not_called()
                self.assertEqual(self.commission_path.read_bytes(), contents)

    def test_shared_first_write_failure_leaves_no_employee_detail(self):
        def interrupted_dump(records, stream, **kwargs):
            self.assertEqual(len(records), 2)
            stream.write('[{"partial":')
            raise OSError(errno.ENOSPC, "simulated disk full")

        with patch.object(commission_store.json, "dump", side_effect=interrupted_dump) as fault:
            code, output = self.invoke(["7", "DEMO-A", "DEMO-B", "2026-10", "100", "30", "y", "0"])
        fault.assert_called_once()
        self.assertEqual(code, 0)
        self.assertIn("提成保存失败", output)
        self.assertNotIn("提成录入成功", output)
        self.assertFalse(self.commission_path.exists())
        self.assertEqual(list(self.commission_path.parent.glob("commissions-*.tmp")), [])

    def test_legacy_query_and_cross_month_reversal_preserve_history(self):
        legacy = [dict(entry_id="OLD", employee_id="DEMO", month="2026-10", commission_cents=123,
                       metadata={"notes": ["中文", "😀"], "flag": True})]
        self.commission_path.parent.mkdir(parents=True, exist_ok=True)
        self.commission_path.write_text(json.dumps(legacy), encoding="utf-8")
        code, output = self.invoke(["8", "2026-10", "9", "OLD", "2026-11", "YES",
                                    "8", "2026-11", "0"])
        self.assertEqual(code, 0)
        self.assertIn("DEMO：1.23 SGD", output)
        self.assertIn("DEMO：-1.23 SGD", output)
        self.assertIn("提成冲回记录已保存", output)
        loaded = commission_store.load_commission_records(self.commission_path)
        self.assertEqual(loaded[:1], legacy)
        self.assertNotIn("audit_version", loaded[0])
        self.assertNotIn("audit_version", loaded[1])
        self.assertEqual(loaded[1]["reversal_of_entry_id"], "OLD")

    def test_duplicate_fields_introduced_during_input_block_cash_save(self):
        storage.save_records(sample_records())
        corrupt = b'[{"type":"income","amount_cents":100,"amount_cents":1,"note":"DEMO"}]'
        values = iter(["1", "10", "NEXT", "4", "0"])

        def respond(prompt):
            value = next(values)
            if value == "NEXT":
                self.path.write_bytes(corrupt)
            return value

        with patch.object(storage, "NamedTemporaryFile") as create:
            code, output = self.invoke(respond)
        self.assertEqual(code, 0)
        self.assertIn("保存失败", output)
        self.assertNotIn("收入录入成功", output)
        self.assertIn("当前余额：75.00 SGD", output)
        create.assert_not_called()
        self.assertEqual(self.path.read_bytes(), corrupt)


if __name__ == "__main__":
    unittest.main()
