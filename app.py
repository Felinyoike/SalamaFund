"""
SalamaFund — Flask Application Gateway
======================================

Exposes the core B2B scoring/disbursement API and serves the operational
partner dashboard.

API
    POST /api/v1/score-and-disburse   Score a farmer and decide disbursement.
    GET  /api/v1/assets               Verified green-tech asset matrix.
    GET  /api/v1/stream               Recent scoring events (powers dashboard).
    GET  /api/v1/health               Liveness + Earth Engine status.

Dashboard (HTML)
    GET  /                            Institutional landing page.
    GET  /dashboard                   Partner portal overview.
    GET  /assessment                  Live risk-assessment console.
    GET  /docs                        API documentation.

The dashboard is fed by an in-memory ring buffer of recent scoring events, so
every API call placed against the gateway streams straight into the UI — no
database required for the hackathon.
"""

from collections import deque

from flask import Flask, render_template, request, jsonify

# flask-cors lets external institutions call the API cross-origin. It's optional:
# if it isn't installed we degrade gracefully rather than crash the gateway.
try:
    from flask_cors import CORS
    _HAS_CORS = True
except ImportError:
    _HAS_CORS = False

from scoring_engine import (
    score_and_disburse,
    validate_payload,
    ASSET_MITIGATION_MATRIX,
    TIER_LIMITS,
    EE_STATUS,
)

app = Flask(__name__)
if _HAS_CORS:
    CORS(app)  # institutions integrate cross-origin; lock this down in production.

# In-memory ring buffer of the most recent scoring events. A deque with a
# bounded maxlen gives us O(1) appends and automatic eviction of stale events.
EVENT_STREAM: "deque[dict]" = deque(maxlen=50)


# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #

@app.route("/api/v1/score-and-disburse", methods=["POST"])
def score_and_disburse_route():
    """Validate the payload, run the scoring pipeline, and record the event."""
    data = request.get_json(silent=True)
    cleaned, error = validate_payload(data)
    if error:
        return jsonify({"status": "error", "message": error}), 400

    try:
        decision = score_and_disburse(cleaned)
    except Exception as exc:  # never leak a 500 to an integrating institution
        return jsonify({"status": "error",
                        "message": f"Scoring pipeline failure: {exc}"}), 500

    # Push a compact summary onto the stream for the live dashboard.
    EVENT_STREAM.appendleft(_summarize(decision))

    return jsonify({"status": "success", "data": decision}), 200


@app.route("/api/v1/assets", methods=["GET"])
def assets_route():
    """Expose the verified green-tech mitigation matrix (drives the UI picker)."""
    assets = [
        {
            "asset_id": aid,
            "name": meta["name"],
            "max_buffer": meta["buffer"],
            "rainfall_decoupling": meta["rainfall_decoupling"],
            "vendor": meta["vendor"],
        }
        for aid, meta in ASSET_MITIGATION_MATRIX.items()
    ]
    return jsonify({"status": "success", "data": assets}), 200


@app.route("/api/v1/stream", methods=["GET"])
def stream_route():
    """Return recent scoring events plus rolling portfolio aggregates."""
    events = list(EVENT_STREAM)
    return jsonify({"status": "success",
                    "data": {"events": events, "aggregates": _aggregates(events)}}), 200


@app.route("/api/v1/health", methods=["GET"])
def health_route():
    """Liveness probe + which climate-data mode we're running in."""
    return jsonify({
        "status": "ok",
        "earth_engine": EE_STATUS,
        "tier_limits": TIER_LIMITS,
    }), 200


# --------------------------------------------------------------------------- #
# Dashboard (HTML) routes
# --------------------------------------------------------------------------- #

@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html", ee_status=EE_STATUS)


@app.route("/assessment")
def assessment():
    return render_template(
        "assessment.html",
        assets=list(ASSET_MITIGATION_MATRIX.items()),
        tier_limits=TIER_LIMITS,
        ee_status=EE_STATUS,
    )


@app.route("/docs")
def docs():
    return render_template("docs.html")


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

def _summarize(decision: dict) -> dict:
    """Flatten a full decision into the compact shape the dashboard consumes."""
    return {
        "farmer_id": decision["farmer_id"],
        "score": decision["climate_credit_score"],
        "tier": decision["risk_tier"],
        "requested": decision["requested_loan_amount_ksh"],
        "approved": decision["approved"],
        "approved_amount": decision["approved_amount_ksh"],
        "asset_name": decision["asset"].get("asset_name"),
        "tier_upgraded": decision["tier_upgraded_by_asset"],
        "spi": decision["environmental"]["spi_anomaly"],
        "drought": decision["environmental"]["drought_exposure"],
        "lat": decision["coordinates"]["latitude"],
        "lon": decision["coordinates"]["longitude"],
        "scored_at": decision["scored_at"],
    }


def _aggregates(events: list[dict]) -> dict:
    """Rolling portfolio KPIs shown on the overview dashboard."""
    if not events:
        return {"total_disbursed": 0.0, "active_loans": 0,
                "avg_score": 0, "approval_rate": 0.0, "low_risk_share": 0.0}
    approved = [e for e in events if e["approved"]]
    total_disbursed = sum(e["approved_amount"] for e in approved)
    avg_score = round(sum(e["score"] for e in events) / len(events))
    approval_rate = round(100.0 * len(approved) / len(events), 1)
    low_risk = [e for e in events if e["tier"] == "LOW"]
    low_risk_share = round(100.0 * len(low_risk) / len(events), 1)
    return {
        "total_disbursed": round(total_disbursed, 2),
        "active_loans": len(approved),
        "avg_score": avg_score,
        "approval_rate": approval_rate,
        "low_risk_share": low_risk_share,
    }


if __name__ == "__main__":
    # Debug mode for live reloads during the hackathon.
    app.run(debug=True, port=5000)
