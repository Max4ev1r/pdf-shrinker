#!/usr/bin/env python3
"""
中国工作日判断脚本（含调休/调班）
逻辑：优先读本地节假日JSON → 回退到 chinese_calendar 库 → 最终回退到 weekday 判断
返回：stdout "WORKDAY" 或 "RESTDAY"，exit code 0=工作日 1=休息日
"""
import sys
import os
import json
from datetime import date
from typing import Optional, Tuple

HERMES_DATA = os.path.expanduser("~/.hermes/data")

def check_local_holidays(d):
    # type: (date) -> Optional[Tuple[bool, str]]
    """检查本地节假日 JSON 文件，返回 (is_workday, reason) 或 None（无数据）"""
    year = d.year
    json_path = os.path.join(HERMES_DATA, "china_holidays_{}.json".format(year))
    if not os.path.isfile(json_path):
        return None

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    date_str = d.isoformat()

    if date_str in data.get("holidays", {}):
        return (False, "节假日：{}".format(data["holidays"][date_str]))

    if date_str in data.get("workdays", {}):
        return (True, "调班日：{}".format(data["workdays"][date_str]))

    is_weekend = d.weekday() >= 5
    return (not is_weekend, "正常工作日" if not is_weekend else "正常周末")


def check_chinese_calendar(d):
    # type: (date) -> Optional[Tuple[bool, str]]
    """使用 chinese_calendar 库判断"""
    try:
        from chinese_calendar import is_workday, get_holiday_detail
        result = is_workday(d)
        if result:
            return (True, "chinese_calendar: 工作日")
        else:
            holiday, name = get_holiday_detail(d)
            return (False, "chinese_calendar: 休息日（{}）".format(name or "周末"))
    except ImportError:
        return None


def main():
    today = date.today()

    result = check_local_holidays(today)
    if result is not None:
        is_work, reason = result
        print("{} ({})".format("WORKDAY" if is_work else "RESTDAY", reason))
        sys.exit(0 if is_work else 1)

    result = check_chinese_calendar(today)
    if result is not None:
        is_work, reason = result
        print("{} ({})".format("WORKDAY" if is_work else "RESTDAY", reason))
        sys.exit(0 if is_work else 1)

    is_weekend = today.weekday() >= 5
    print("{} (fallback: weekday={})".format("RESTDAY" if is_weekend else "WORKDAY", today.weekday()))
    sys.exit(1 if is_weekend else 0)


if __name__ == "__main__":
    main()
