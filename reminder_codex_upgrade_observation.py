#!/usr/bin/env python3
"""No-agent Codex upgrade observation reminder."""


def main() -> None:
    print(
        "提醒 Max：Codex 7天观察期已到，可以进入升级第二阶段。\n\n"
        "📋 观察期到期 checklist（请逐项确认）：\n\n"
        "1. Hindsight 连续7天健康，shadow report无报错\n"
        "2. RSS常驻稳定，未长期超过1.5-2GB\n"
        "3. learning review连续7天抓到真实纠错/偏好，非噪音\n"
        "4. 微信主链路、cron、检索速度未受影响\n"
        "5. 至少抽查3份报告，候选项\"有用率\"超过60%\n\n"
        "✅ 全部通过 → 进入第二阶段：\n"
        "- 生成 learning-actions 队列\n"
        "- 每条action标记风险等级（auto-safe / needs-review）\n"
        "- auto-safe项（记忆去重、错别字、非高风险偏好）可自动落地\n"
        "- needs-review项（补剂、健康、产品结论、专家规则）只生成待确认\n"
        "- 落地前后生成diff和回滚备份\n\n"
        "❌ 任一项不达标 → 先优化审计规则，不进入第二阶段"
    )


if __name__ == "__main__":
    main()
