"""清洗契约（spec）的加载与校验。

契约是口径的唯一来源：阈值、白名单、基准日、产物清单都在这里。
代码里不允许内联阈值；需要新口径先升 spec_version。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .api import PanelSpec, SpecError

SPEC_VERSION_PATTERN = r"^\d+\.\d+\.\d+$"


class HardValidation(BaseModel):
    model_config = ConfigDict(extra="allow")

    rules: list[str] = Field(min_length=1)
    code_whitelist: list[str] = Field(min_length=1)
    violation_rate_max: float = Field(ge=0.0, le=1.0, default=0.001)
    violation_rate_fatal: float = Field(ge=0.0, le=1.0, default=0.01)
    on_fail: str = "L2"

    @model_validator(mode="after")
    def _fatal_ge_max(self) -> "HardValidation":
        if self.violation_rate_fatal < self.violation_rate_max:
            raise ValueError("violation_rate_fatal must be >= violation_rate_max")
        return self


class PriceAdjustment(BaseModel):
    model_config = ConfigDict(extra="allow")

    qfq_base_date: date
    factor_definition: str = "factor = close_hfq / close_raw"
    any_base_rule: str = "adj(b) = close_raw * factor / factor[b]"
    reproduction_tolerance: float = Field(gt=0, default=1e-3)


class SpecDoc(BaseModel):
    """spec/cleaning.spec.json 的运行时模型。"""

    model_config = ConfigDict(extra="allow")

    spec_version: str = Field(pattern=SPEC_VERSION_PATTERN)
    timezone: str = Field(min_length=1)
    hard_validation: HardValidation
    price_adjustment: PriceAdjustment
    missing: dict[str, Any] = Field(default_factory=dict)
    lookahead: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)

    def to_panel_spec(self) -> PanelSpec:
        return PanelSpec(
            spec_version=self.spec_version,
            timezone=self.timezone,
            code_whitelist=tuple(self.hard_validation.code_whitelist),
            violation_rate_max=self.hard_validation.violation_rate_max,
            violation_rate_fatal=self.hard_validation.violation_rate_fatal,
            base_date=self.price_adjustment.qfq_base_date,
            raw=self.model_dump(mode="json"),
        )


def load_spec(path: str | Path) -> SpecDoc:
    """读取并校验契约；任何结构问题统一抛 SpecError。"""
    p = Path(path)
    if not p.exists():
        raise SpecError(f"spec not found: {p}")
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecError(f"spec is not valid json: {exc}") from exc
    if not isinstance(payload, dict):
        raise SpecError("spec root must be an object")
    try:
        return SpecDoc.model_validate(payload)
    except ValidationError as exc:
        raise SpecError(f"invalid spec: {exc.errors()}") from exc


def dump_spec(doc: SpecDoc) -> str:
    return json.dumps(doc.model_dump(mode="json"), ensure_ascii=False, indent=2)
