"""Retention-period destruction orchestration.

A destruction request is judged against the versions present at submission
time. The service is intentionally thin: eligibility checks and the actual
deletion happen inside the store's single SQLite transaction, so a capture
that lands a not-yet-due revision between the check and the commit forces a
rollback. Repeating a destruction for the same announcement returns the
original certificate instead of creating a second one.
"""
from __future__ import annotations

from .domain import (
    DestructionCertificate,
    DestructionResult,
    cutoff_to_iso,
    normalize_number,
    parse_utc_cutoff,
)
from .store import ArchiveStore


class DestructionService:
    def __init__(self, store: ArchiveStore):
        self.store = store

    def destroy(self, raw_number: str, raw_cutoff: str) -> DestructionResult:
        number = normalize_number(raw_number)
        cutoff = parse_utc_cutoff(raw_cutoff)
        cutoff_iso = cutoff_to_iso(cutoff)

        existing = self.store.get_certificate(number)
        if existing is not None:
            # Idempotent: repeat handling must return the original voucher.
            return DestructionResult(
                status="already_destroyed",
                certificate=existing,
                message=(
                    "该公告已办理过销毁，返回原销毁凭证；"
                    "不会重复销毁，也不会产生新凭证。"
                ),
            )

        certificate = self.store.destroy_announcement(number, cutoff_iso)
        return DestructionResult(
            status="destroyed",
            certificate=certificate,
            message=(
                f"销毁完成：公告 {number} 的全部 {certificate.version_count} "
                "个版本正文与附件已删除，销毁凭证已留存（不含原件）。"
            ),
        )

    def certificate_for(self, raw_number: str) -> DestructionCertificate | None:
        return self.store.get_certificate(normalize_number(raw_number))
