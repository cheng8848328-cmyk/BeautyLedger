def calculate_service_commission(original_price_cents, rate_bps, completed):
    """按项目原价计算整数分提成；调用方应传原价，而非折后实收金额。

    rate_bps 必须显式提供，10000 基点表示 100%。所有参数均先校验，
    未完成服务返回 0；已完成服务若产生不足一分的结果则拒绝计算。
    类型错误抛出 TypeError，取值越界或无法精确到分抛出 ValueError。
    """
    if type(original_price_cents) is not int:
        raise TypeError("original_price_cents 必须是整数分，不能是 bool")
    if original_price_cents <= 0:
        raise ValueError("original_price_cents 必须大于 0")
    if type(rate_bps) is not int:
        raise TypeError("rate_bps 必须是整数基点，不能是 bool")
    if not 0 <= rate_bps <= 10000:
        raise ValueError("rate_bps 必须在 0 至 10000 之间")
    if type(completed) is not bool:
        raise TypeError("completed 必须是 bool")

    if not completed:
        return 0

    commission_cents, remainder = divmod(original_price_cents * rate_bps, 10000)
    if remainder:
        raise ValueError("提成结果含不足一分的金额，舍入规则尚未确认")
    return commission_cents


def calculate_topup_commission(topup_amount_cents):
    """按固定 500 基点计算充值销售提成，金额和返回值均为整数分。

    充值金额必须为正整数且不能是 bool；不足一分时抛出 ValueError，
    等待业务舍入规则确认。本函数仅计算，不执行任何账务或文件操作。
    """
    if type(topup_amount_cents) is not int:
        raise TypeError("topup_amount_cents 必须是整数分，不能是 bool")
    if topup_amount_cents <= 0:
        raise ValueError("topup_amount_cents 必须大于 0")

    commission_cents, remainder = divmod(topup_amount_cents * 500, 10000)
    if remainder:
        raise ValueError("提成结果含不足一分的金额，舍入规则尚未确认")
    return commission_cents


def split_commission_equally(total_commission_cents):
    """将已计算的非负整数分总提成平分给两人，返回两个整数的元组。

    不计算提成比例，不执行文件或员工记录操作；奇数分的平分舍入规则
    尚未确认，暂时抛出 ValueError。bool 和其他非整数类型均不接受。
    """
    if type(total_commission_cents) is not int:
        raise TypeError("total_commission_cents 必须是整数分，不能是 bool")
    if total_commission_cents < 0:
        raise ValueError("total_commission_cents 必须大于或等于 0")

    share_cents, remainder = divmod(total_commission_cents, 2)
    if remainder:
        raise ValueError("总提成为奇数分，平分舍入规则尚未确认")
    return share_cents, share_cents


def summarize_employee_commissions(entries, month):
    """按月份汇总已确认的整数分提成，返回员工 ID 到净额的字典。

    校验全部记录，entry_id 在整个列表内必须唯一；额外字段忽略。
    月份须为 ASCII YYYY-MM（0001-01 至 9999-12），ID 不得为空白。
    保留零净额，允许负数调整；不生成退款、不修改输入或执行 I/O。
    类型错误抛出 TypeError，缺失字段或非法取值抛出 ValueError。
    """
    def validate_month(value):
        if type(value) is not str:
            raise TypeError("month 必须是 YYYY-MM 字符串")
        if (len(value) != 7 or value[4] != "-"
                or any(char not in "0123456789" for char in value[:4] + value[5:])
                or not 1 <= int(value[:4]) <= 9999
                or not 1 <= int(value[5:]) <= 12):
            raise ValueError("month 必须是合法的 YYYY-MM 月份")

    if type(entries) is not list:
        raise TypeError("entries 必须是字典组成的列表")
    validate_month(month)
    required_fields = {"entry_id", "employee_id", "month", "commission_cents"}
    seen_ids = set()
    totals = {}
    for entry in entries:
        if type(entry) is not dict:
            raise TypeError("每条提成记录必须是字典")
        if not required_fields.issubset(entry):
            raise ValueError("提成记录缺少必需字段")
        for field in ("entry_id", "employee_id"):
            if type(entry[field]) is not str:
                raise TypeError(f"{field} 必须是非空字符串")
            if not entry[field].strip():
                raise ValueError(f"{field} 不能为空或纯空白")
        validate_month(entry["month"])
        amount = entry["commission_cents"]
        if type(amount) is not int:
            raise TypeError("commission_cents 必须是整数分，不能是 bool")
        if entry["entry_id"] in seen_ids:
            raise ValueError("entry_id 重复，不能重复累计提成")
        seen_ids.add(entry["entry_id"])
        if entry["month"] == month:
            employee_id = entry["employee_id"]
            totals[employee_id] = totals.get(employee_id, 0) + amount
    return totals


def create_full_commission_reversal(
        original_entry, reversal_entry_id, reversal_month, existing_entries):
    """为一条已确认的正向提成生成全额负数冲回，不修改或保存输入。

    原明细的四个必需字段必须与列表中的记录一致；冲回月份显式指定。
    依据本次列表检查 ID 冲突和已有冲回引用，调用方须提供完整最新记录。
    仅处理单条员工提成，不执行退款支付、工资扣回或整笔交易退款。
    """
    # 复用现有结构、类型、月份及唯一性校验，不使用汇总结果。
    summarize_employee_commissions([original_entry], reversal_month)
    summarize_employee_commissions(existing_entries, reversal_month)
    if original_entry["commission_cents"] <= 0:
        raise ValueError("原提成金额必须大于 0，不能冲回零金额或负数调整")
    if "reversal_of_entry_id" in original_entry:
        raise ValueError("原明细必须是正向提成，不能是冲回记录")

    reversal = {
        "entry_id": reversal_entry_id,
        "employee_id": original_entry["employee_id"],
        "month": reversal_month,
        "commission_cents": -original_entry["commission_cents"],
        "reversal_of_entry_id": original_entry["entry_id"],
    }
    summarize_employee_commissions([reversal], reversal_month)
    stored_original = None
    for entry in existing_entries:
        if entry["entry_id"] == reversal_entry_id:
            raise ValueError("冲回 entry_id 已存在")
        if "reversal_of_entry_id" in entry:
            reference = entry["reversal_of_entry_id"]
            if type(reference) is not str:
                raise TypeError("reversal_of_entry_id 必须是非空字符串")
            if not reference.strip():
                raise ValueError("reversal_of_entry_id 不能为空或纯空白")
            if reference == original_entry["entry_id"]:
                raise ValueError("原明细已有冲回记录，不能重复冲回")
        if entry["entry_id"] == original_entry["entry_id"]:
            stored_original = entry

    if stored_original is None:
        raise ValueError("原明细不存在于 existing_entries")
    if "reversal_of_entry_id" in stored_original:
        raise ValueError("原明细必须是正向提成，不能是冲回记录")
    for field in ("entry_id", "employee_id", "month", "commission_cents"):
        if stored_original[field] != original_entry[field]:
            raise ValueError(f"原明细关键字段不一致：{field}")
    return reversal
