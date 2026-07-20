#!/usr/bin/env python3
"""No-agent evening skincare reminder — Phase 3 optimized.

Changes from Phase 3:
  - Mon/Wed/Fri adapalene: NO buffer (direct application, wait 15-20min)
  - Sun: changed from rest to azelaic acid (4x/week vs adapalene 3x/week)

Schedule:
  Mon/Wed/Fri   → adapalene night (direct, no buffer)
  Tue/Thu/Sat/Sun → azelaic acid night
"""

from datetime import datetime


def main() -> None:
    weekday = datetime.now().weekday()  # 0=Mon ... 6=Sun

    if weekday in (0, 2, 4):  # Mon, Wed, Fri
        print(
            "🌙 晚间护肤提醒（阿达帕林夜）\n\n"
            "1. 洁面（CeraVe）\n"
            "2. 阿达帕林0.1%（达芙文）豌豆大小全脸薄涂\n"
            "3. ⏰ 等待15-20分钟吸收\n"
            "4. PM乳（CeraVe PM）保湿\n\n"
            "已耐受，取消缓冲法，直接涂抹提升效果。今晚不用壬二酸。"
        )
    else:  # Tue, Thu, Sat, Sun
        print(
            "🌙 晚间护肤提醒（壬二酸夜）\n\n"
            "1. 洁面（CeraVe）\n"
            "2. 壬二酸15%（Finacea）薄涂\n"
            "3. ⏰ 等5-10分钟吸收\n"
            "4. PM乳（CeraVe PM）保湿（如感觉干燥）\n\n"
            "今晚不用阿达帕林。"
        )


if __name__ == "__main__":
    main()
