"""独立的提成 JSON 存储，仅支持单进程串行读写。

默认数据及临时文件位于 Git 忽略的 data/ 下。自定义路径由调用方管理。
原子替换防止半写文件，但不提供并发锁、版本检查、备份或断电恢复保证；
保存前检查完整历史前缀，拒绝过期或改写历史的快照；检查与替换之间
的并发更新仍可能被覆盖。此模块不生成或批准退款。
"""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from commission import (
    calculate_service_commission,
    calculate_topup_commission,
    split_commission_equally,
    summarize_employee_commissions,
)


COMMISSIONS_FILE = Path(__file__).resolve().parent / "data" / "commissions.json"


def _validate_audit_records(records, require_audit):
    """审计版按完整交易验证；无版本且无计算依据的历史记录保持原样。"""
    service_fields = {"original_price_cents", "rate_bps", "completed", "participant_count"}
    basis_fields = service_fields | {"topup_amount_cents"}
    transactions = {}
    audited_transactions = set()
    for record in records:
        if "transaction_id" in record:
            transaction_id = record["transaction_id"]
            if type(transaction_id) is not str or not transaction_id.strip():
                raise ValueError("transaction_id 必须是非空字符串")
            transactions.setdefault(transaction_id, []).append(record)
        audited = "audit_version" in record or bool(basis_fields.intersection(record))
        if not audited:
            if require_audit:
                raise ValueError("新录入记录必须包含 audit_version 和完整计算依据")
            continue
        if type(record.get("audit_version")) is not int or record["audit_version"] != 1:
            raise ValueError("audit_version 必须为整数 1")
        if record.get("entry_type") not in ("service", "topup"):
            raise ValueError("审计记录 entry_type 必须为 service 或 topup")
        if "transaction_id" not in record:
            raise ValueError("审计记录缺少 transaction_id")
        if "reversal_of_entry_id" in record or record["commission_cents"] < 0:
            raise ValueError("计算依据保留在正向原记录，冲回通过 entry_id 引用")
        if record["entry_type"] == "service":
            if not service_fields.issubset(record) or "topup_amount_cents" in record:
                raise ValueError("服务记录缺少计算依据或混入充值依据")
            count = record["participant_count"]
            if type(count) is not int or count not in (1, 2):
                raise ValueError("participant_count 必须为整数 1 或 2")
        elif "topup_amount_cents" not in record or service_fields.intersection(record):
            raise ValueError("充值记录缺少充值金额或混入服务依据")
        audited_transactions.add(record["transaction_id"])

    for transaction_id in audited_transactions:
        group = transactions[transaction_id]
        first = group[0]
        if any(record.get("audit_version") != 1 for record in group):
            raise ValueError("同一审计交易不能混入旧格式记录")
        common = {"entry_type", "month"}
        if first["entry_type"] == "service":
            common |= service_fields
            count = first["participant_count"]
        else:
            common.add("topup_amount_cents")
            count = 1
        if len(group) != count or len({record["employee_id"] for record in group}) != count:
            raise ValueError("交易明细数量或员工数量与分配人数不一致")
        if any(record.get(field) != first[field] for record in group for field in common):
            raise ValueError("同一交易的类型、月份或计算依据不一致")
        # 每条都验证严格类型，避免 True == 1 或整数 == 浮点数绕过校验。
        for record in group:
            if record["entry_type"] == "service":
                total = calculate_service_commission(
                    record["original_price_cents"], record["rate_bps"], record["completed"])
                expected = split_commission_equally(total)[0] if count == 2 else total
            else:
                expected = calculate_topup_commission(record["topup_amount_cents"])
            if record["commission_cents"] != expected:
                raise ValueError("提成金额与计算依据或分配方式不一致")


def validate_commission_records(records, *, require_audit=False):
    """全量校验，不修改输入；非法数据统一抛出 ValueError。

    非负记录不得带冲回引用。负数必须全额冲回同员工的正向记录，
    每条原记录只能冲回一次；引用按整个数据集解析，不依赖列表顺序。
    require_audit=True 用于 CLI 新录入；默认兼容无计算依据的旧记录。
    审计校验仅比较计算结果，不补字段、不覆盖任何历史金额。
    """
    try:
        summarize_employee_commissions(records, "0001-01")
        _validate_audit_records(records, require_audit)
    except (TypeError, ValueError) as error:
        raise ValueError(str(error)) from error

    by_id = {record["entry_id"]: record for record in records}
    reversed_ids = set()
    for record in records:
        amount = record["commission_cents"]
        if amount >= 0:
            if "reversal_of_entry_id" in record:
                raise ValueError("非负提成不能带 reversal_of_entry_id")
            continue
        reference = record.get("reversal_of_entry_id")
        if type(reference) is not str or not reference.strip():
            raise ValueError("负数提成必须带合法的 reversal_of_entry_id")
        original = by_id.get(reference)
        if (original is None or original["commission_cents"] <= 0
                or "reversal_of_entry_id" in original):
            raise ValueError("冲回必须引用同一数据集内的正向提成")
        if (amount != -original["commission_cents"]
                or record["employee_id"] != original["employee_id"]):
            raise ValueError("冲回金额或员工与原提成不匹配")
        if reference in reversed_ids:
            raise ValueError("同一原提成不能重复冲回")
        reversed_ids.add(reference)

    # 额外字段必须可无损 JSON 往返，拒绝元组、非字符串键等隐式转换。
    try:
        encoded = json.dumps(records, ensure_ascii=False, allow_nan=False)
        if json.loads(encoded, object_pairs_hook=_unique_object) != records:
            raise ValueError("JSON 往返会改变记录内容")
    except (TypeError, ValueError) as error:
        raise ValueError("提成记录包含无法保存的 JSON 数据") from error


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 对象字段重复：{key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"非法 JSON 常量：{value}")


def load_commission_records(path=COMMISSIONS_FILE):
    """仅文件不存在时返回 []；空文件、损坏内容和读取错误均不忽略。"""
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as file:
            records = json.load(file, object_pairs_hook=_unique_object,
                                parse_constant=_reject_constant)
    except FileNotFoundError:
        return []
    validate_commission_records(records)
    return records


def save_commission_records(records, path=COMMISSIONS_FILE):
    """校验新旧数据及历史前缀，仅允许原样保存或在末尾追加。

    同目录临时文件写完、flush/fsync 并关闭后才 os.replace。
    替换前失败保留旧文件，尽力清理本次临时文件；不修改调用者数据。
    拒绝覆盖损坏文件；前缀检查不提供检查与替换之间的并发保护。
    """
    validate_commission_records(records)
    path = Path(path)
    existing = load_commission_records(path)
    # 不能只比较 Python 字典相等：额外字段中的 True、1、1.0 也不可互换。
    if (len(records) < len(existing)
            or json.dumps(records[:len(existing)], sort_keys=True, allow_nan=False)
            != json.dumps(existing, sort_keys=True, allow_nan=False)):
        raise ValueError("已有提成历史必须原样保留且顺序不变，只能在末尾追加记录")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix="commissions-", suffix=".tmp", delete=False,
        ) as file:
            temporary_path = Path(file.name)
            json.dump(records, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                # 替换成功后路径已不存在；清理失败不掩盖原始错误。
                pass
