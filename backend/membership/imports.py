"""
기존 고객 CSV 일괄 등록 — payhere 등 타 시스템 회원 이관.

이관 원칙:
- 초기 포인트는 원장(PointEntry, reason=adjust)에 기록해 "원장=진실의 원천"을 유지
- 등급은 원본 표기와 무관하게 누적 결제액으로 재계산
- 이미 등록된 연락처는 건너뛴다(기존 데이터를 덮어쓰지 않음)
- dry_run=True 면 검증·집계만 하고 DB에 아무것도 쓰지 않는다

CSV 형식: 첫 행이 헤더. 이름·연락처만 필수, 나머지 열은 선택.
한국 엑셀 저장 파일(CP949)과 UTF-8(BOM 포함) 모두 지원.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from django.db import transaction as db_transaction
from django.utils import timezone

from .models import Member, PointEntry, Store

MAX_FILE_BYTES = 1_000_000  # 1MB — 220명 이관에 충분, 오업로드 방지
MAX_ROWS = 2_000

# 헤더 자동 매핑: payhere 내보내기 등 다양한 열 이름을 수용
HEADER_ALIASES = {
    "name": {"이름", "성명", "고객명", "고객이름", "닉네임", "name"},
    "phone": {
        "연락처", "전화번호", "휴대폰", "휴대폰번호", "핸드폰", "핸드폰번호",
        "휴대전화", "전화", "회원번호", "phone",
    },
    "points": {"포인트", "보유포인트", "잔여포인트", "잔여적립금", "적립금", "points"},
    "total_spent": {
        "누적결제", "누적결제액", "총결제액", "누적금액", "총이용금액",
        "총구매금액", "total_spent",
    },
    "visit_count": {"방문", "방문횟수", "방문수", "이용횟수", "visit_count"},
    "stamps": {"스탬프", "스탬프수", "stamps"},
    "marketing_opt_in": {
        "마케팅동의", "마케팅수신동의", "마케팅수신", "수신동의",
        "opt_in", "marketing_opt_in",
    },
    "joined_at": {"가입일", "가입일자", "등록일", "최초방문일", "joined_at"},
}

TRUTHY = {"y", "yes", "true", "1", "o", "동의", "동의함", "수신", "수신동의"}

DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d",
)


class CsvImportError(Exception):
    """파일 단위 실패(형식·인코딩·헤더). 행 단위 문제는 results에 담는다."""


def decode_csv_bytes(data: bytes) -> str:
    """한국 엑셀(CP949)·UTF-8(BOM) 자동 판별 디코딩."""
    if len(data) > MAX_FILE_BYTES:
        raise CsvImportError("파일이 너무 큽니다(최대 1MB).")
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvImportError("파일 인코딩을 읽을 수 없습니다. CSV(UTF-8 또는 엑셀 저장 형식)로 내보내 주세요.")


def _normalize_header(cell: str) -> str:
    # "연락처(휴대폰)" → "연락처", 공백·BOM 제거, 영문은 소문자
    cell = (cell or "").replace("﻿", "").strip().lower()
    cell = re.sub(r"[(\[].*$", "", cell)
    return re.sub(r"\s+", "", cell)


def _map_headers(header_row: list[str]) -> dict[int, str]:
    """열 인덱스 → 필드명. 이름·연락처 열이 없으면 파일 단위 실패."""
    mapping: dict[int, str] = {}
    for idx, cell in enumerate(header_row):
        key = _normalize_header(cell)
        for field, aliases in HEADER_ALIASES.items():
            if key in aliases and field not in mapping.values():
                mapping[idx] = field
                break
    missing = {"name", "phone"} - set(mapping.values())
    if missing:
        raise CsvImportError(
            "헤더에서 이름·연락처 열을 찾을 수 없습니다. "
            "첫 행에 '이름'과 '연락처'(또는 전화번호) 열을 포함해 주세요."
        )
    return mapping


def normalize_phone(raw: str) -> str | None:
    """숫자만 남기고 010 형식으로 정규화. 유효하지 않으면 None."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("82"):
        digits = "0" + digits[2:]
    if digits.startswith("01") and 10 <= len(digits) <= 11:
        return digits
    return None


def _parse_int(raw: str) -> int:
    # "1,234원" "500P" 등 표기 수용
    s = re.sub(r"[^\d-]", "", str(raw or ""))
    return int(s) if s and s != "-" else 0


def _parse_bool(raw: str) -> bool:
    return str(raw or "").strip().lower() in TRUTHY


def _parse_datetime(raw: str):
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        return dt
    return None


def parse_rows(csv_text: str) -> list[dict]:
    """CSV 텍스트 → 필드 매핑된 행 목록(1-기반 행 번호 포함)."""
    reader = csv.reader(io.StringIO(csv_text))
    try:
        header = next(reader)
    except StopIteration:
        raise CsvImportError("빈 파일입니다.")
    mapping = _map_headers(header)

    rows: list[dict] = []
    for line_no, cells in enumerate(reader, start=2):
        if not any((c or "").strip() for c in cells):
            continue  # 빈 줄 무시
        row = {"_row": line_no}
        for idx, field in mapping.items():
            row[field] = cells[idx].strip() if idx < len(cells) else ""
        rows.append(row)
        if len(rows) > MAX_ROWS:
            raise CsvImportError(f"행이 너무 많습니다(최대 {MAX_ROWS}행).")
    if not rows:
        raise CsvImportError("등록할 데이터 행이 없습니다.")
    return rows


def import_members_csv(
    *, file_bytes: bytes | None = None, csv_text: str | None = None, dry_run: bool = False
) -> dict:
    """
    CSV 일괄 등록 실행. 반환: 행별 결과와 집계.

    행 상태: created(등록) / skipped(이미 등록된 연락처·파일 내 중복) / error(검증 실패).
    dry_run이면 동일한 검증·집계를 수행하되 DB에 쓰지 않는다.
    """
    store = Store.objects.first()
    if store is None:
        raise CsvImportError("매장 설정이 없습니다. seed_demo를 실행하세요.")

    if csv_text is None:
        csv_text = decode_csv_bytes(file_bytes or b"")
    rows = parse_rows(csv_text)

    # 정규화된 연락처 기준으로 기존 회원·파일 내 중복을 한 번에 판별
    normalized = [(r, normalize_phone(r.get("phone", ""))) for r in rows]
    phones = [p for _, p in normalized if p]
    existing = set(
        Member.objects.filter(phone__in=phones).values_list("phone", flat=True)
    )

    results: list[dict] = []
    to_create: list[dict] = []
    seen: set[str] = set()

    for row, phone in normalized:
        name = row.get("name", "").strip()
        entry = {"row": row["_row"], "name": name, "phone": phone or row.get("phone", "")}
        if phone is None:
            entry.update(status="error", reason="연락처 형식이 올바르지 않습니다(01로 시작, 10~11자리).")
            results.append(entry)
            continue
        if phone in existing:
            entry.update(status="skipped", reason="이미 등록된 회원입니다.")
            results.append(entry)
            continue
        if phone in seen:
            entry.update(status="skipped", reason="파일 안에 같은 연락처가 중복됩니다.")
            results.append(entry)
            continue
        seen.add(phone)

        if not name:
            name = f"고객{phone[-4:]}"  # 이름 없는 행도 이관은 가능하게
            entry["name"] = name
        data = {
            "phone": phone,
            "name": name[:50],
            "points": max(0, _parse_int(row.get("points", ""))),
            "total_spent": max(0, _parse_int(row.get("total_spent", ""))),
            "visit_count": max(0, _parse_int(row.get("visit_count", ""))),
            "stamps": max(0, _parse_int(row.get("stamps", ""))),
            "marketing_opt_in": _parse_bool(row.get("marketing_opt_in", "")),
            "joined_at": _parse_datetime(row.get("joined_at", "")),
        }
        entry.update(status="created", reason="", points=data["points"])
        results.append(entry)
        to_create.append(data)

    if not dry_run and to_create:
        _persist(store, to_create)

    counts = {"created": 0, "skipped": 0, "error": 0}
    for r in results:
        counts[r["status"]] += 1
    return {
        "dry_run": dry_run,
        "total": len(results),
        "created": counts["created"],
        "skipped": counts["skipped"],
        "errors": counts["error"],
        "results": results,
    }


@db_transaction.atomic
def _persist(store: Store, to_create: list[dict]) -> None:
    """검증 통과분을 원자적으로 등록. 초기 포인트는 원장에 남긴다."""
    for data in to_create:
        joined_at = data.pop("joined_at")
        points = data.pop("points")
        member = Member.objects.create(store=store, points=points, **data)
        member.tier = member.compute_tier()
        member.save(update_fields=["tier"])
        if points > 0:
            PointEntry.objects.create(
                member=member,
                delta=points,
                reason=PointEntry.Reason.ADJUST,
                balance_after=points,
            )
        if joined_at is not None:
            # auto_now_add 우회 — 이관 시 원래 가입일 보존
            Member.objects.filter(pk=member.pk).update(joined_at=joined_at)
