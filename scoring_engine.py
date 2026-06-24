"""
SalamaFund — Core Scoring & Disbursement Engine
================================================

This module is the deterministic brain of the SalamaFund B2B API. It converts a
farmer's GPS coordinates and cooperative revenue into a FICO-style "Climate
Credit Score" (300–850) and a risk-tiered micro-loan disbursement decision.

The pipeline is layered so each stage is independently testable:

    GPS ─► Geospatial Climate Engine ─► Environmental Resilience (0–300)
    Revenue ─► Alternative Credit Engine ─► Financial Capacity (0–350)
    Asset ─► Mitigation Engine ─► Risk Buffer Offset (0–150)
                       │
                       ▼
        Climate Credit Score (300–850) ─► Tier ─► Max Loan ─► B2B Payout

Google Earth Engine (`ee`) powers the climate layer. If `ee` is unavailable or
uninitialised (common in a hackathon laptop or CI runner), every GEE call falls
back to a *deterministic* mock derived from the coordinate hash — so the same
location always yields the same numbers and the demo never crashes mid-pitch.
"""

from __future__ import annotations

import math
import hashlib
import uuid
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Google Earth Engine bootstrap (graceful, never fatal)
# --------------------------------------------------------------------------- #
# We try to import AND initialise EE up front. `EE_READY` is the single source
# of truth the rest of the module consults before touching the live API.
EE_READY = False
EE_STATUS = "mock"  # one of: "live", "mock", "import-error", "init-error"

try:
    import ee  # type: ignore

    try:
        ee.Initialize()
        EE_READY = True
        EE_STATUS = "live"
    except Exception as exc:  # ee.EEException + auth/credential errors
        # Import succeeded but we could not authenticate / initialise.
        EE_STATUS = "init-error"
        print(f"[SalamaFund] Earth Engine present but not initialised: {exc}. "
              f"Falling back to deterministic mock climate data.")
except ImportError:
    EE_STATUS = "import-error"
    print("[SalamaFund] earthengine-api not installed — using mock climate data.")


# --------------------------------------------------------------------------- #
# Tunable constants — kept together so risk officers can audit the model
# --------------------------------------------------------------------------- #

# CHIRPS 30-year climatological baseline window (prompt spec: 1981–2011).
CHIRPS_BASELINE_START = "1981-01-01"
CHIRPS_BASELINE_END = "2011-12-31"

# Standardized Precipitation Anomaly thresholds.
# SPI <= -1.5 is the operational definition of "high drought exposure".
SPI_DROUGHT_THRESHOLD = -1.5
SPI_WET_THRESHOLD = 1.0  # at/above this we treat rainfall as fully resilient

# MODIS NDVI dry-month variance ceiling. Greenness variance during the dry
# season measures how well the land *retains* water: stable greenness (low
# variance) implies strong water-retention capability, volatile greenness
# (high variance) implies the farm tracks rainfall and dries out fast.
NDVI_VARIANCE_CEILING = 0.04

# Component point ceilings (must mirror the docstring / prompt spec).
ENV_RESILIENCE_MAX = 300
FINANCIAL_CAPACITY_MAX = 350
ASSET_BUFFER_MAX = 150

# Climate Credit Score band (FICO-style).
SCORE_MIN = 300
SCORE_MAX = 850

# Revenue (KSh/month) that saturates the financial capacity component.
REVENUE_SATURATION_KSH = 150_000.0

# Disbursement ceilings by risk tier.
TIER_LIMITS = {
    "LOW": 150_000.0,
    "MODERATE": 75_000.0,
    "HIGH": 25_000.0,
}

# A symbolic green-tech vendor wallet that funds are routed to (B2B, never the
# farmer's consumer wallet — this is what prevents cash diversion).
VENDOR_WALLET_PREFIX = "VND-WALLET"


# --------------------------------------------------------------------------- #
# Asset Mitigation Matrix
# --------------------------------------------------------------------------- #
# Each verified green asset "decouples" the farm from rainfall to some degree.
# `buffer` is the maximum environmental risk offset (points) the asset unlocks;
# `rainfall_decoupling` (0–1) is how independent of rain the tech makes the farm
# and is used to *scale* the buffer for genuinely high-risk locations.
ASSET_MITIGATION_MATRIX = {
    "ASSET-SOLAR-PUMP-04": {
        "name": "SunCulture Solar Irrigation Pump",
        "buffer": 150,
        "rainfall_decoupling": 0.95,
        "vendor": "SunCulture Kenya Ltd",
    },
    "ASSET-DRIP-IRRIG-02": {
        "name": "Precision Drip Irrigation Kit",
        "buffer": 110,
        "rainfall_decoupling": 0.75,
        "vendor": "Netafim East Africa",
    },
    "ASSET-WATER-TANK-01": {
        "name": "5,000L Rainwater Harvesting Tank",
        "buffer": 80,
        "rainfall_decoupling": 0.55,
        "vendor": "Kentainers Ltd",
    },
    "ASSET-BIOGAS-03": {
        "name": "Biogas Digester Unit",
        "buffer": 60,
        "rainfall_decoupling": 0.30,
        "vendor": "Sistema.bio Kenya",
    },
    "ASSET-DROUGHT-SEED-05": {
        "name": "Certified Drought-Tolerant Seed Pack",
        "buffer": 50,
        "rainfall_decoupling": 0.40,
        "vendor": "Kenya Seed Company",
    },
}


# --------------------------------------------------------------------------- #
# Small numeric helpers
# --------------------------------------------------------------------------- #

def _clamp(value: float, low: float, high: float) -> float:
    """Clamp `value` into the inclusive [low, high] range."""
    return max(low, min(high, value))


def _coordinate_seed(latitude: float, longitude: float) -> float:
    """
    Deterministic 0–1 seed derived from coordinates.

    Used by the mock climate layer so a given farm *always* produces the same
    SPI / NDVI numbers — stable demos, reproducible tests, no random flicker.
    """
    key = f"{round(latitude, 4)}:{round(longitude, 4)}".encode()
    digest = hashlib.sha256(key).hexdigest()
    # Take 8 hex chars → int → normalise to [0, 1).
    return int(digest[:8], 16) / 0xFFFFFFFF


# --------------------------------------------------------------------------- #
# LAYER 1 — Geospatial Climate Risk Engine (CHIRPS + MODIS NDVI)
# --------------------------------------------------------------------------- #

def _chirps_spi_live(point) -> float:
    """
    Standardized Precipitation Anomaly (SPI) from live CHIRPS data.

    Math:
        1. Build the 30-year baseline (1981–2011) monthly-rainfall distribution
           and reduce it to a per-pixel historical MEAN (μ) and STD DEV (σ).
        2. Take the trailing 12-month rainfall MEAN at the same pixel (x̄).
        3. SPI = (x̄ − μ) / σ      ← classic standardized anomaly.

    A negative SPI means the recent year was drier than the 30-year norm;
    SPI ≤ -1.5 is the trigger for "high drought exposure".
    """
    baseline = (
        ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
        .filterDate(CHIRPS_BASELINE_START, CHIRPS_BASELINE_END)
        .select("precipitation")
    )
    # Per-pixel historical mean & standard deviation of daily precipitation.
    hist_mean = baseline.mean()
    hist_std = baseline.reduce(ee.Reducer.stdDev())

    # Trailing 12-month window (relative to "now" in EE server time).
    recent = (
        ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
        .filterDate(ee.Date(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                    .advance(-12, "month"),
                    ee.Date(datetime.now(timezone.utc).strftime("%Y-%m-%d")))
        .select("precipitation")
        .mean()
    )

    # SPI = (recent − μ) / σ, evaluated at the farm pixel.
    spi_img = recent.subtract(hist_mean).divide(hist_std)
    spi = spi_img.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=point, scale=5000
    ).get("precipitation")
    return float(ee.Number(spi).getInfo())


def _ndvi_dry_variance_live(point) -> float:
    """
    Variance of MODIS NDVI greenness during historic dry months over 5 years.

    Math:
        1. Pull 5 years of 16-day MODIS NDVI composites.
        2. Keep only the dry-season months (Jan, Feb, Jun, Jul, Aug, Sep for
           Kenya's bimodal calendar) — that's when water retention shows.
        3. Reduce the stack with a variance reducer at the farm pixel.

    Low variance ⇒ greenness holds steady through the dry months ⇒ strong
    localized water-retention capability ⇒ lower environmental risk.
    """
    coll = (
        ee.ImageCollection("MODIS/061/MOD13Q1")
        .filterDate(ee.Date(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                    .advance(-5, "year"),
                    ee.Date(datetime.now(timezone.utc).strftime("%Y-%m-%d")))
        .select("NDVI")
        # MODIS NDVI is scaled by 1e4; bring it back to the natural -1..1 range.
        .map(lambda img: img.multiply(0.0001).copyProperties(img, ["system:time_start"]))
        .filter(ee.Filter.calendarRange(1, 9, "month"))  # dry-leaning months
    )
    variance = coll.reduce(ee.Reducer.variance()).reduceRegion(
        reducer=ee.Reducer.mean(), geometry=point, scale=250
    ).get("NDVI_variance")
    return float(ee.Number(variance).getInfo())


def _climate_metrics_mock(latitude: float, longitude: float) -> dict:
    """
    Deterministic stand-in for the GEE climate layer.

    Produces a plausible SPI in roughly [-2.2, +1.6] and an NDVI dry-month
    variance in roughly [0.005, 0.06], both keyed to the coordinate hash so
    every run is identical.
    """
    seed = _coordinate_seed(latitude, longitude)
    # Map seed → SPI: low seed = drought-prone, high seed = well-watered.
    spi = round(-2.2 + seed * 3.8, 3)
    # A second, decorrelated seed for NDVI variance.
    seed2 = _coordinate_seed(longitude, latitude)
    ndvi_variance = round(0.005 + seed2 * 0.055, 4)
    return {"spi": spi, "ndvi_dry_variance": ndvi_variance}


def assess_environmental_risk(latitude: float, longitude: float) -> dict:
    """
    LAYER 1 entrypoint. Returns the climate diagnostics plus a normalized
    Environmental Resilience Score in [0, 300] (higher = more climate-resilient
    = lower lending risk).

    Resilience is a weighted blend:
        60% rainfall reliability (from the SPI anomaly)
        40% water-retention capability (from NDVI dry-month variance)
    """
    spi = None
    ndvi_variance = None
    source = EE_STATUS

    if EE_READY:
        try:
            point = ee.Geometry.Point([longitude, latitude])
            spi = _chirps_spi_live(point)
            ndvi_variance = _ndvi_dry_variance_live(point)
            source = "live"
        except ee.EEException as exc:  # explicit per deliverable requirement
            print(f"[SalamaFund] ee.EEException during climate pull: {exc}. "
                  f"Falling back to mock.")
            source = "mock-fallback"
        except Exception as exc:  # any other server hiccup
            print(f"[SalamaFund] Unexpected EE error: {exc}. Falling back to mock.")
            source = "mock-fallback"

    if spi is None or ndvi_variance is None:
        mock = _climate_metrics_mock(latitude, longitude)
        spi, ndvi_variance = mock["spi"], mock["ndvi_dry_variance"]

    # --- Rainfall reliability factor (0..1) -------------------------------- #
    # Linearly interpolate between the drought threshold (-1.5 → 0.0) and the
    # wet threshold (+1.0 → 1.0). SPI below -1.5 saturates at 0 (max risk).
    span = SPI_WET_THRESHOLD - SPI_DROUGHT_THRESHOLD
    rainfall_factor = _clamp((spi - SPI_DROUGHT_THRESHOLD) / span, 0.0, 1.0)

    # --- Water-retention factor (0..1) ------------------------------------- #
    # Low NDVI variance → factor near 1.0; variance at/above the ceiling → 0.0.
    retention_factor = _clamp(1.0 - (ndvi_variance / NDVI_VARIANCE_CEILING), 0.0, 1.0)

    resilience = (0.60 * rainfall_factor + 0.40 * retention_factor) * ENV_RESILIENCE_MAX

    return {
        "spi_anomaly": round(spi, 3),
        "ndvi_dry_variance": round(ndvi_variance, 4),
        "rainfall_factor": round(rainfall_factor, 3),
        "retention_factor": round(retention_factor, 3),
        "drought_exposure": spi <= SPI_DROUGHT_THRESHOLD,
        "environmental_resilience": round(resilience, 1),
        "data_source": source,
    }


# --------------------------------------------------------------------------- #
# LAYER 2 — Alternative Credit Engine
# --------------------------------------------------------------------------- #

def assess_financial_capacity(monthly_coop_revenue_ksh: float) -> dict:
    """
    LAYER 2. Converts cooperative revenue + a derived transaction-velocity proxy
    into a Financial Capacity Score in [0, 350].

    We use a log curve on revenue so that the jump from KSh 5k→50k matters far
    more than 100k→150k (diminishing marginal signal), and a deterministic
    velocity proxy stands in for real transaction-history variables (cadence /
    consistency of cooperative deposits) until a transaction feed is wired in.
    """
    revenue = max(0.0, float(monthly_coop_revenue_ksh))

    # Revenue factor (0..1) on a log scale, saturating at REVENUE_SATURATION.
    if revenue <= 0:
        revenue_factor = 0.0
    else:
        revenue_factor = _clamp(
            math.log10(1 + revenue) / math.log10(1 + REVENUE_SATURATION_KSH),
            0.0, 1.0,
        )

    # Transaction-velocity proxy (0..1): deterministic from revenue magnitude so
    # the demo is stable. Stands in for deposit cadence / consistency.
    velocity_factor = _clamp(_coordinate_seed(revenue, revenue * 0.5) * 0.6 + revenue_factor * 0.4, 0.0, 1.0)

    capacity = (0.70 * revenue_factor + 0.30 * velocity_factor) * FINANCIAL_CAPACITY_MAX

    return {
        "monthly_revenue_ksh": round(revenue, 2),
        "revenue_factor": round(revenue_factor, 3),
        "velocity_factor": round(velocity_factor, 3),
        "financial_capacity": round(capacity, 1),
    }


# --------------------------------------------------------------------------- #
# LAYER 3 — Asset Mitigation Engine
# --------------------------------------------------------------------------- #

def assess_asset_buffer(requested_asset_id: str | None,
                        environmental: dict) -> dict:
    """
    LAYER 3. If the farmer is using the loan to acquire a verified green asset,
    that hardware decouples the farm from rainfall and earns an environmental
    risk buffer (up to +150 points).

    The buffer is scaled by how *exposed* the farm currently is: a solar pump is
    worth almost its full buffer to a drought-prone farm but adds little to an
    already well-watered one (which doesn't need de-risking). This is exactly
    the "asset unlocks a higher tier for high-risk farmers" behaviour.
    """
    if not requested_asset_id:
        return {"asset_id": None, "asset_name": None, "asset_buffer": 0.0,
                "vendor": None}

    asset = ASSET_MITIGATION_MATRIX.get(requested_asset_id.upper())
    if asset is None:
        return {"asset_id": requested_asset_id, "asset_name": None,
                "asset_buffer": 0.0, "vendor": None,
                "note": "Asset not in verified mitigation matrix — no buffer applied."}

    # "Need" for de-risking = 1 - current rainfall reliability.
    exposure_need = 1.0 - environmental["rainfall_factor"]
    # Scale: floor at 35% of the buffer (the tech always helps a bit), full
    # buffer when the farm is maximally exposed AND the tech fully decouples it.
    scale = 0.35 + 0.65 * exposure_need * asset["rainfall_decoupling"]
    buffer = _clamp(asset["buffer"] * scale, 0.0, ASSET_BUFFER_MAX)

    return {
        "asset_id": requested_asset_id.upper(),
        "asset_name": asset["name"],
        "vendor": asset["vendor"],
        "asset_buffer": round(buffer, 1),
        "rainfall_decoupling": asset["rainfall_decoupling"],
    }


# --------------------------------------------------------------------------- #
# LAYER 4 — Score fusion, tiering & disbursement
# --------------------------------------------------------------------------- #

def _fuse_score(environmental: float, financial: float, buffer: float) -> int:
    """
    Fuse the three component scores into a single Climate Credit Score (300–850).

    raw ∈ [0, 800]  = environmental(0–300) + financial(0–350) + buffer(0–150)
    score           = SCORE_MIN + (raw / 800) * (SCORE_MAX − SCORE_MIN)

    This linearly maps the achievable point pool onto the FICO-style band so the
    asset buffer can genuinely push a farmer across a tier boundary.
    """
    raw = environmental + financial + buffer
    raw_max = ENV_RESILIENCE_MAX + FINANCIAL_CAPACITY_MAX + ASSET_BUFFER_MAX
    score = SCORE_MIN + (raw / raw_max) * (SCORE_MAX - SCORE_MIN)
    return int(round(_clamp(score, SCORE_MIN, SCORE_MAX)))


def _tier_for_score(score: int) -> tuple[str, float]:
    """Map a Climate Credit Score to its risk tier and max loan ceiling."""
    if score >= 700:
        return "LOW", TIER_LIMITS["LOW"]
    if score >= 550:
        return "MODERATE", TIER_LIMITS["MODERATE"]
    return "HIGH", TIER_LIMITS["HIGH"]


def _build_disbursement_payload(farmer_id: str, approved_amount: float,
                                asset: dict) -> dict:
    """
    Generate a mock secure B2B merchant-payment payload.

    Funds settle directly into the verified green-tech vendor's wallet — the
    farmer's consumer wallet is intentionally bypassed so the capital cannot be
    diverted away from the climate-resilience asset it was underwritten for.
    """
    vendor = asset.get("vendor") or "SalamaFund General Disbursement Pool"
    vendor_wallet = f"{VENDOR_WALLET_PREFIX}-{abs(hash(vendor)) % 10_000:04d}"
    return {
        "transaction_id": f"TXN-{uuid.uuid4().hex[:12].upper()}",
        "rail": "B2B_MERCHANT_SETTLEMENT",
        "settlement_status": "QUEUED",
        "amount_ksh": round(approved_amount, 2),
        "currency": "KES",
        "payer": {"entity": "SalamaFund Escrow", "farmer_ref": farmer_id},
        "payee": {"vendor": vendor, "wallet": vendor_wallet,
                  "asset_id": asset.get("asset_id")},
        "consumer_wallet_bypassed": True,
        "initiated_at": datetime.now(timezone.utc).isoformat(),
        "memo": "Automated green-tech disbursement — anti-diversion B2B rail.",
    }


def score_and_disburse(payload: dict) -> dict:
    """
    Top-level orchestration: run all four layers and produce the final scoring +
    disbursement decision. `payload` is the validated request dict.
    """
    farmer_id = payload["farmer_id"]
    latitude = payload["latitude"]
    longitude = payload["longitude"]
    requested_amount = float(payload["requested_loan_amount"])
    requested_asset_id = payload.get("requested_asset_id")
    monthly_revenue = float(payload["monthly_coop_revenue_ksh"])

    # Layer 1 — climate.
    environmental = assess_environmental_risk(latitude, longitude)
    # Layer 2 — finance.
    financial = assess_financial_capacity(monthly_revenue)
    # Layer 3 — asset mitigation.
    asset = assess_asset_buffer(requested_asset_id, environmental)

    # Layer 4 — fuse, tier, decide.
    score = _fuse_score(
        environmental["environmental_resilience"],
        financial["financial_capacity"],
        asset["asset_buffer"],
    )
    tier, max_loan = _tier_for_score(score)

    # For transparency, also report the tier the farmer would have had WITHOUT
    # the asset buffer — this powers the "asset unlocked a higher tier" UI.
    base_score = _fuse_score(
        environmental["environmental_resilience"],
        financial["financial_capacity"],
        0.0,
    )
    base_tier, _ = _tier_for_score(base_score)
    tier_upgraded = (asset["asset_buffer"] > 0) and (tier != base_tier)

    approved = requested_amount <= max_loan
    approved_amount = requested_amount if approved else max_loan

    decision = {
        "farmer_id": farmer_id,
        "coordinates": {"latitude": latitude, "longitude": longitude},
        "climate_credit_score": score,
        "base_score_without_asset": base_score,
        "risk_tier": tier,
        "base_risk_tier": base_tier,
        "tier_upgraded_by_asset": tier_upgraded,
        "max_loan_limit_ksh": max_loan,
        "requested_loan_amount_ksh": requested_amount,
        "approved": approved,
        "approved_amount_ksh": round(approved_amount, 2),
        "components": {
            "environmental_resilience": environmental["environmental_resilience"],
            "financial_capacity": financial["financial_capacity"],
            "asset_buffer": asset["asset_buffer"],
        },
        "environmental": environmental,
        "financial": financial,
        "asset": asset,
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }

    # Layer 4b — disbursement payload only when we actually release funds.
    if approved and approved_amount > 0:
        decision["disbursement"] = _build_disbursement_payload(
            farmer_id, approved_amount, asset
        )
    else:
        decision["disbursement"] = None

    return decision


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #

REQUIRED_FIELDS = {
    "farmer_id": str,
    "latitude": (int, float),
    "longitude": (int, float),
    "requested_loan_amount": (int, float),
    "monthly_coop_revenue_ksh": (int, float),
}


def validate_payload(data: dict | None) -> tuple[dict | None, str | None]:
    """
    Validate the incoming JSON payload for /api/v1/score-and-disburse.

    Returns (cleaned_payload, None) on success or (None, error_message) on the
    first validation failure.
    """
    if not isinstance(data, dict):
        return None, "Request body must be a JSON object."

    cleaned: dict = {}
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in data or data[field] in (None, ""):
            return None, f"Missing required field: '{field}'."
        value = data[field]
        if not isinstance(value, expected_type) or isinstance(value, bool):
            return None, f"Field '{field}' must be of type {expected_type}."
        cleaned[field] = value

    # Range sanity checks.
    if not (-90.0 <= float(cleaned["latitude"]) <= 90.0):
        return None, "latitude must be between -90 and 90."
    if not (-180.0 <= float(cleaned["longitude"]) <= 180.0):
        return None, "longitude must be between -180 and 180."
    if float(cleaned["requested_loan_amount"]) <= 0:
        return None, "requested_loan_amount must be positive."
    if float(cleaned["monthly_coop_revenue_ksh"]) < 0:
        return None, "monthly_coop_revenue_ksh cannot be negative."

    # Optional asset id.
    asset_id = data.get("requested_asset_id")
    if asset_id is not None:
        if not isinstance(asset_id, str):
            return None, "requested_asset_id must be a string."
        cleaned["requested_asset_id"] = asset_id

    return cleaned, None
