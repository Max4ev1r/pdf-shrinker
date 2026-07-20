#!/usr/bin/env python3
"""No-agent morning skincare reminder — Phase 3 optimized (niacinamide 3x/week)."""

from datetime import datetime


def main() -> None:
    weekday = datetime.now().weekday()  # 0=Mon ... 6=Sun

    if weekday in (0, 2, 4):  # Mon, Wed, Fri
        print(
            "☀️ 早间护肤提醒（烟酰胺日）\n\n"
            "1. 洁面（CeraVe氨基酸泡沫洁面 / 温水）\n"
            "2. 烟酰胺10%+锌1%（The Ordinary）薄涂\n"
            "3. ⏰ 等1-2分钟吸收\n"
            "4. 防晒SPF50+（理肤泉大哥大400）足量涂\n\n"
            "烟酰胺每周一三五使用，其余天只做洁面+防晒。"
        )
    else:  # Tue, Thu, Sat, Sun
        print(
            "☀️ 早间护肤提醒\n\n"
            "1. 洁面（CeraVe氨基酸泡沫洁面 / 温水）\n"
            "2. 防晒SPF50+（理肤泉大哥大400）足量涂\n\n"
            "今天不用烟酰胺，洁面+防晒即可。感觉拔干可薄涂保湿。"
        )


if __name__ == "__main__":
    main()
