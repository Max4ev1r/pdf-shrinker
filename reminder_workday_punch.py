#!/usr/bin/env python3
"""No-agent workday punch reminder."""

from datetime import date

import check_workday


def is_workday(today: date) -> bool:
    result = check_workday.check_local_holidays(today)
    if result is not None:
        return bool(result[0])

    result = check_workday.check_chinese_calendar(today)
    if result is not None:
        return bool(result[0])

    return today.weekday() < 5


def main() -> None:
    if is_workday(date.today()):
        print("5:30 了，别忘了打卡。")


if __name__ == "__main__":
    main()
