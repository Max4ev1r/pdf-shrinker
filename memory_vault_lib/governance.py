"""Pure governance rules and record invariants for the memory vault."""

from __future__ import annotations

import difflib
import re
from typing import Any, Callable


DEFAULT_HIGH_RISK_TOPICS = {
    "health",
    "company_finance",
}
DEFAULT_HIGH_RISK_PATTERNS = (
    "高血压", "用药", "替尔泊肽", "体检", "护肤", "BMI", "宝宝", "家人",
    "公司", "财税", "社保", "股权", "黄金", "持仓", "股票", "基金", "证券",
    "券商", "买入价", "成交额", "资产", "收入", "工资", "贷款", "债务", "余额",
    "身份证", "密码", "token", "secret", "api key", "令牌", "密钥", "银行卡",
)
SECRET_PATTERNS = (
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        re.IGNORECASE,
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"\b(?:ghp|github_pat|glpat|xox[baprs])_[A-Za-z0-9_-]{16,}\b"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\."
        r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    ),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|"
        r"password|passwd|secret)\s*[:=]\s*[\"']?"
        r"[A-Za-z0-9._~+/=-]{8,}",
        re.IGNORECASE,
    ),
)


class SecretMemoryRejected(ValueError):
    """Raised before a credential-like value reaches durable storage."""


class MemoryGovernance:
    """Evaluate candidates and enforce authority-store record invariants."""

    def __init__(
        self,
        *,
        valid_statuses: set[str],
        id_factory: Callable[[str, str], str],
        title_factory: Callable[[str], str],
        content_hash_factory: Callable[[str], str],
        high_risk_topics: set[str] | None = None,
        high_risk_patterns: tuple[str, ...] | None = None,
    ) -> None:
        self.valid_statuses = valid_statuses
        self.id_factory = id_factory
        self.title_factory = title_factory
        self.content_hash_factory = content_hash_factory
        self.high_risk_topics = (
            high_risk_topics or DEFAULT_HIGH_RISK_TOPICS
        )
        self.high_risk_patterns = (
            high_risk_patterns or DEFAULT_HIGH_RISK_PATTERNS
        )

    @staticmethod
    def normalize(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    @staticmethod
    def contains_secret(body: str) -> bool:
        return any(pattern.search(body or "") for pattern in SECRET_PATTERNS)

    def assert_memory_safe(self, body: str) -> None:
        if self.contains_secret(body):
            raise SecretMemoryRejected(
                "Credential-like content was rejected before entering "
                "durable memory."
            )

    def unique_record_id(
        self,
        records: list[dict[str, Any]],
        namespace: str,
        key: str,
    ) -> str:
        used = {str(record.get("id", "")) for record in records}
        candidate = self.id_factory(namespace, key)
        revision = 1
        while candidate in used:
            revision += 1
            candidate = self.id_factory(
                namespace,
                f"{key}:revision:{revision}",
            )
        return candidate

    @staticmethod
    def record_predecessor_id(record: dict[str, Any]) -> str:
        source = record.get("source")
        if isinstance(source, dict) and source.get("supersedes"):
            return str(source["supersedes"])
        if record.get("governance_action") == "merge":
            return str(record.get("matched_id", ""))
        return ""

    def validate_record_invariants(
        self,
        records: list[dict[str, Any]],
    ) -> None:
        ids = [str(record.get("id", "")) for record in records]
        if not all(ids) or len(ids) != len(set(ids)):
            raise ValueError("Vault records must have non-empty unique IDs")
        invalid = [
            str(record["id"])
            for record in records
            if record.get("status") not in self.valid_statuses
        ]
        if invalid:
            raise ValueError(
                "Vault records have invalid statuses: "
                + ", ".join(invalid)
            )
        secret_ids = [
            str(record["id"])
            for record in records
            if self.contains_secret(str(record.get("body", "")))
        ]
        if secret_ids:
            raise SecretMemoryRejected(
                "Credential-like content exists in durable records: "
                + ", ".join(secret_ids)
            )
        active_ids = {
            str(record["id"])
            for record in records
            if record.get("status") == "active"
        }
        conflicts = []
        for record in records:
            if record.get("status") != "active":
                continue
            predecessor = self.record_predecessor_id(record)
            if predecessor and predecessor in active_ids:
                conflicts.append(f"{record['id']}->{predecessor}")
        if conflicts:
            raise ValueError(
                "A memory version and the version it supersedes cannot both "
                "be active: " + ", ".join(conflicts)
            )

    def body_similarity(self, left: str, right: str) -> float:
        left_normalized = self.normalize(left).lower()
        right_normalized = self.normalize(right).lower()
        if not left_normalized or not right_normalized:
            return 0.0
        return difflib.SequenceMatcher(
            None,
            left_normalized,
            right_normalized,
        ).ratio()

    def nearest_active(
        self,
        records: list[dict[str, Any]],
        body: str,
        *,
        topic: str = "",
    ) -> dict[str, Any] | None:
        best: tuple[float, dict[str, Any] | None] = (0.0, None)
        for record in records:
            if record.get("status") != "active":
                continue
            if topic and record.get("topic") != topic:
                continue
            score = max(
                self.body_similarity(body, str(record.get("body", ""))),
                self.body_similarity(
                    body,
                    str(record.get("summary", "")),
                ),
                self.body_similarity(
                    self.title_factory(body),
                    str(record.get("title", "")),
                ),
            )
            if score > best[0]:
                best = (score, record)
        if best[1] is None:
            return None
        result = dict(best[1])
        result["_similarity"] = round(best[0], 3)
        return result

    def is_high_risk_memory(self, body: str, topic: str) -> bool:
        if topic in self.high_risk_topics:
            return True
        lowered = body.lower()
        return any(
            pattern.lower() in lowered
            for pattern in self.high_risk_patterns
        )

    def decision(
        self,
        body: str,
        records: list[dict[str, Any]],
        *,
        topic: str,
    ) -> dict[str, Any]:
        digest = self.content_hash_factory(body)
        duplicate = next(
            (
                record for record in records
                if record.get("status") in {"pending", "active"}
                and record.get("content_hash") == digest
            ),
            None,
        )
        similar = (
            self.nearest_active(records, body, topic=topic)
            or self.nearest_active(records, body)
        )
        similarity = (
            float(similar.get("_similarity", 0.0))
            if similar
            else 0.0
        )
        high_risk = self.is_high_risk_memory(body, topic)
        if duplicate:
            return {
                "action": "duplicate",
                "risk": "low",
                "review_status": "rejected",
                "matched_id": duplicate.get("id", ""),
                "confidence": 1.0,
                "reason": "same content hash already exists",
            }
        if similar and similarity >= 0.86:
            return {
                "action": "merge",
                "risk": "high" if high_risk else "medium",
                "review_status": (
                    "needs_user_review" if high_risk else "pending"
                ),
                "matched_id": similar.get("id", ""),
                "confidence": similarity,
                "reason": (
                    f"similar to active memory {similar.get('id')} "
                    f"({similarity:.3f})"
                ),
            }
        if high_risk:
            return {
                "action": "needs_user_review",
                "risk": "high",
                "review_status": "needs_user_review",
                "matched_id": similar.get("id", "") if similar else "",
                "confidence": similarity,
                "reason": "high-risk topic or sensitive content",
            }
        return {
            "action": "add",
            "risk": "low",
            "review_status": "pending",
            "matched_id": (
                similar.get("id", "")
                if similar and similarity >= 0.72
                else ""
            ),
            "confidence": similarity,
            "reason": "low-risk durable memory candidate",
        }
