import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from json_validation import canonical_json, load_json, validate_json_value


RECORDS_FILE = Path(__file__).resolve().parent / "data" / "records.json"


def validate_records(records):
    """检查账本和每条记录的格式，不改变原数据。"""
    validate_json_value(records)
    if not isinstance(records, list):
        raise ValueError("账本必须是列表")

    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"第{index}条记录必须是字典")
        if record.get("type") not in ("income", "expense"):
            raise ValueError(f"第{index}条记录的 type 必须是 income 或 expense")

        amount = record.get("amount_cents")
        if type(amount) is not int or amount <= 0:
            raise ValueError(f"第{index}条记录的 amount_cents 必须是大于0的整数分")
        if not isinstance(record.get("note"), str):
            raise ValueError(f"第{index}条记录的 note 必须是文字")


def load_records():
    """读取账本；只有文件不存在时才返回空列表。"""
    try:
        with RECORDS_FILE.open("r", encoding="utf-8") as file:
            records = load_json(file)
    except FileNotFoundError:
        return []

    validate_records(records)
    return records


def save_records(records):
    """保留磁盘完整历史，仅允许原样保存或追加，再以临时文件替换。

    保存前重读并校验旧文件，拒绝覆盖损坏内容或过期快照。
    仅支持单进程串行操作；检查与替换之间没有并发锁。
    """
    validate_records(records)
    existing = load_records()
    # 按 JSON 内容比较历史，忽略对象键顺序，但区分 bool/int/float。
    if (len(records) < len(existing)
            or canonical_json(records[:len(existing)]) != canonical_json(existing)):
        raise ValueError("已有收支历史必须原样保留且顺序不变，只能在末尾追加记录")
    RECORDS_FILE.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=RECORDS_FILE.parent,
            prefix="records-",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            json.dump(records, file, ensure_ascii=False, indent=2, allow_nan=False)

        os.replace(temporary_path, RECORDS_FILE)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("收支记录无法编码为 JSON") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                # 替换成功时临时路径已不存在；清理失败不掩盖原始保存错误。
                pass
