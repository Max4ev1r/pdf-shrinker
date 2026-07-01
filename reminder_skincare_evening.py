#!/usr/bin/env python3
"""No-agent evening skincare reminder — adapalene / azelaic acid alternating."""

from datetime import date


ADAPALENE_START = date(2026, 6, 16)


def main() -> None:
    day = max(1, (date.today() - ADAPALENE_START).days + 1)
    # Odd days = adapalene, even days = azelaic acid
    if day % 2 == 1:
        product = (
            "🌙 晚间护肤提醒（阿达帕林夜）\n\n"
            "1. 洁面（CeraVe）→ 等脸干10分钟\n"
            "2. 阿达帕林0.1%（达芙文）豌豆大小全脸薄涂\n"
            "3. 等5分钟\n"
            "4. PM乳（CeraVe PM）保湿\n\n"
            f"阿达帕林第{day}天。今晚不用壬二酸。"
        )
    else:
        product = (
            "🌙 晚间护肤提醒（壬二酸夜）\n\n"
            "1. 洁面（CeraVe）→ 等脸干\n"
            "2. 壬二酸15%（Finacea）薄涂\n"
            "3. 等5分钟\n"
            "4. PM乳（CeraVe PM）保湿\n\n"
            f"阿达帕林第{day}天。今晚不用阿达帕林。"
        )
    print(product)


if __name__ == "__main__":
    main()
