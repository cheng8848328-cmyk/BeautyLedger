"""两种账本共用的 JSON 安全规则；不转换输入，也不执行文件写入。"""

import json
import math


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 对象包含重复字段")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("JSON 不允许 NaN 或 Infinity")


def _validate_types(value):
    # 显式栈避免校验器自身依赖递归深度；只检查当前祖先链中的循环。
    # 同一非循环对象被多个字段引用是合法的，不应误判为循环。
    stack = [(value, False)]
    ancestors = set()
    while stack:
        current, leaving = stack.pop()
        if leaving:
            ancestors.remove(id(current))
            continue
        kind = type(current)
        if current is None or kind in (bool, int):
            continue
        if kind is str:
            current.encode("utf-8")
        elif kind is float:
            if not math.isfinite(current):
                raise ValueError("JSON 数值必须有限")
        elif kind in (list, dict):
            identity = id(current)
            if identity in ancestors:
                raise ValueError("JSON 数据不能包含循环引用")
            ancestors.add(identity)
            stack.append((current, True))
            if kind is dict:
                for key, child in current.items():
                    if type(key) is not str:
                        raise ValueError("JSON 对象字段名必须是字符串")
                    key.encode("utf-8")
                    stack.append((child, False))
            else:
                stack.extend((child, False) for child in current)
        else:
            raise ValueError("数据包含不支持的 JSON 类型")


def validate_json_value(value):
    """拒绝隐式转换和不可编码内容；数据错误统一为 ValueError。

    有限浮点数仅作为普通 JSON 元数据允许，金额规则由业务层检查。
    不设固定深度上限；解释器编码深度或整数转换限制仍可能拒绝数据。
    """
    try:
        _validate_types(value)
        # 与保存采用相同的编码选项，提前验证实际编码能力。
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("数据不是可安全保存的 UTF-8 JSON") from error


def load_json(file):
    """严格读取 JSON；不吞掉 I/O 错误，不对异常数据进行自动修复。"""
    try:
        value = json.load(file, object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("JSON 内容无效或嵌套过深") from error
    # 也拒绝 1e400 解析产生的无穷大，以及转义后出现的孤立 surrogate。
    validate_json_value(value)
    return value


def canonical_json(value):
    """用于历史比较：忽略对象键顺序，保留列表顺序及 JSON 数值类型。"""
    validate_json_value(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("JSON 历史内容无法比较") from error
