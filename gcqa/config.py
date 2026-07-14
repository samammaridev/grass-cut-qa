"""Configuration loading: config/qa.yaml (ops-tunable) + .env (credentials).

Loaded once at startup per run — no live reload (ORCHESTRATION.md §14). The
sha256 of the raw yaml bytes is stamped into every audit record as
config_version so any run can be traced to the exact config that produced it.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ApiCredentials:
    auth_url: str
    auth_user: str
    auth_password: str
    api_base: str
    image_base: str


@dataclass(frozen=True)
class Thresholds:
    auto_decide: dict[str, float]
    verifier_below_confidence: float


@dataclass(frozen=True)
class Gates:
    min_fraud_photo_evidence: int
    fraud_requires_deterministic_signal: bool
    fraud_requires_verifier_agreement: bool
    verifier_runs_on: list[str]


@dataclass(frozen=True)
class Prechecks:
    min_time_on_site_min: int
    gps_outlier_m: float
    date_skew_days: int
    min_review_photos: int
    min_photo_success_ratio: float
    min_gps_points: int
    gps_property_match_m: float
    gps_property_fail_km: float
    gps_mixed_cluster_min_points: int
    phash_max_distance: int = 4
    min_before_after_gap_min: int = 5
    cross_station_adjacency_s: int = 30


@dataclass(frozen=True)
class Geocode:
    timeout_s: float
    retries: int
    rate_limit_s: float
    nominatim_enabled: bool
    multi_match_radius_m: float
    cache_path: str
    cache_ttl_days: int
    negative_ttl_days: int


@dataclass(frozen=True)
class Runtime:
    concurrency: int
    max_budget_usd_per_order: float
    max_turns: int
    photo_long_edge_px: int
    jpeg_quality: int
    retention_days: int
    max_thinking_tokens: int = 8000


@dataclass(frozen=True)
class CostTargets:
    per_order_usd_warn: float = 0.20
    per_order_usd_fail: float = 0.35
    monthly_orders: int = 30000


@dataclass(frozen=True)
class Prompts:
    system: str
    verifier_suffix: str
    fewshots: str


@dataclass(frozen=True)
class Report:
    max_words: int
    lint: bool


@dataclass(frozen=True)
class Config:
    models: dict[str, str]
    thresholds: Thresholds
    gates: Gates
    prechecks: Prechecks
    geocode: Geocode
    runtime: Runtime
    prompts: Prompts
    report: Report
    cost_targets: CostTargets
    batch: dict
    config_version: str
    root: Path = field(default_factory=Path.cwd)


_REQUIRED_TOP_KEYS = (
    "models", "thresholds", "gates", "prechecks", "geocode",
    "runtime", "prompts", "report",
)


def load_config(path: Path | str = "config/qa.yaml") -> Config:
    path = Path(path)
    raw = path.read_bytes()
    data = yaml.safe_load(raw)
    missing = [k for k in _REQUIRED_TOP_KEYS if k not in data]
    if missing:
        raise ConfigError(f"config {path} missing required keys: {missing}")
    version = "sha256:" + hashlib.sha256(raw).hexdigest()[:16]
    try:
        return Config(
            models=dict(data["models"]),
            thresholds=Thresholds(
                auto_decide={k: float(v) for k, v in data["thresholds"]["auto_decide"].items()},
                verifier_below_confidence=float(data["thresholds"]["verifier_below_confidence"]),
            ),
            gates=Gates(
                min_fraud_photo_evidence=int(data["gates"]["min_fraud_photo_evidence"]),
                fraud_requires_deterministic_signal=bool(data["gates"]["fraud_requires_deterministic_signal"]),
                fraud_requires_verifier_agreement=bool(data["gates"]["fraud_requires_verifier_agreement"]),
                verifier_runs_on=list(data["gates"]["verifier_runs_on"]),
            ),
            prechecks=Prechecks(
                min_time_on_site_min=int(data["prechecks"]["min_time_on_site_min"]),
                gps_outlier_m=float(data["prechecks"]["gps_outlier_m"]),
                date_skew_days=int(data["prechecks"]["date_skew_days"]),
                min_review_photos=int(data["prechecks"]["min_review_photos"]),
                min_photo_success_ratio=float(data["prechecks"]["min_photo_success_ratio"]),
                min_gps_points=int(data["prechecks"]["min_gps_points"]),
                gps_property_match_m=float(data["prechecks"]["gps_property_match_m"]),
                gps_property_fail_km=float(data["prechecks"]["gps_property_fail_km"]),
                gps_mixed_cluster_min_points=int(data["prechecks"]["gps_mixed_cluster_min_points"]),
                phash_max_distance=int(data["prechecks"].get("phash_max_distance", 4)),
                min_before_after_gap_min=int(data["prechecks"].get("min_before_after_gap_min", 5)),
                cross_station_adjacency_s=int(data["prechecks"].get("cross_station_adjacency_s", 30)),
            ),
            geocode=Geocode(
                timeout_s=float(data["geocode"]["timeout_s"]),
                retries=int(data["geocode"]["retries"]),
                rate_limit_s=float(data["geocode"]["rate_limit_s"]),
                nominatim_enabled=bool(data["geocode"]["nominatim_enabled"]),
                multi_match_radius_m=float(data["geocode"]["multi_match_radius_m"]),
                cache_path=str(data["geocode"]["cache_path"]),
                cache_ttl_days=int(data["geocode"]["cache_ttl_days"]),
                negative_ttl_days=int(data["geocode"]["negative_ttl_days"]),
            ),
            runtime=Runtime(
                concurrency=int(data["runtime"]["concurrency"]),
                max_budget_usd_per_order=float(data["runtime"]["max_budget_usd_per_order"]),
                max_turns=int(data["runtime"]["max_turns"]),
                photo_long_edge_px=int(data["runtime"]["photo_long_edge_px"]),
                jpeg_quality=int(data["runtime"]["jpeg_quality"]),
                retention_days=int(data["runtime"]["retention_days"]),
                max_thinking_tokens=int(data["runtime"].get("max_thinking_tokens", 8000)),
            ),
            cost_targets=CostTargets(
                per_order_usd_warn=float(data.get("cost_targets", {}).get("per_order_usd_warn", 0.20)),
                per_order_usd_fail=float(data.get("cost_targets", {}).get("per_order_usd_fail", 0.35)),
                monthly_orders=int(data.get("cost_targets", {}).get("monthly_orders", 30000)),
            ),
            prompts=Prompts(
                system=str(data["prompts"]["system"]),
                verifier_suffix=str(data["prompts"]["verifier_suffix"]),
                fewshots=str(data["prompts"]["fewshots"]),
            ),
            report=Report(
                max_words=int(data["report"]["max_words"]),
                lint=bool(data["report"]["lint"]),
            ),
            batch=dict(data.get("batch", {"mode": "sdk"})),
            config_version=version,
            root=path.resolve().parent.parent,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"config {path} invalid: {e!r}") from e


_ENV_KEYS = (
    "SAFEGUARD_AUTH_URL", "SAFEGUARD_AUTH_USER", "SAFEGUARD_AUTH_PASSWORD",
    "SAFEGUARD_API_BASE", "SAFEGUARD_IMAGE_BASE",
)


def load_env(dotenv: Path | str = ".env") -> ApiCredentials:
    """Load SAFEGUARD_* credentials from process env, falling back to a .env file.

    Fail-fast on anything missing — a half-configured pipeline must not start.
    """
    values: dict[str, str] = {}
    dotenv = Path(dotenv)
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    for key in _ENV_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    missing = [k for k in _ENV_KEYS if not values.get(k)]
    if missing:
        raise ConfigError(f"missing credentials {missing} (checked env + {dotenv})")
    return ApiCredentials(
        auth_url=values["SAFEGUARD_AUTH_URL"],
        auth_user=values["SAFEGUARD_AUTH_USER"],
        auth_password=values["SAFEGUARD_AUTH_PASSWORD"],
        api_base=values["SAFEGUARD_API_BASE"].rstrip("/"),
        image_base=values["SAFEGUARD_IMAGE_BASE"].rstrip("/"),
    )
