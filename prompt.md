Act as an expert Backend Architect and Geospatial Data Engineer specializing in FinTech and Climate-Smart Agriculture infrastructure. I have already started working on a B2B API gateway called "SalamaFund" using Python and Flask. I need you to refine, optimize, and write the complete, production-ready implementation for our core scoring and disbursement backend, along with structured guidance for an operational dashboard.

### Project Context & Core Objective
SalamaFund is an AI-powered B2B credit-scoring and disbursement API that plugs into existing financial institutions (SACCOs and MFIs) in Kenya. It replaces traditional physical collateral with geospatial climate-risk data and alternative transaction history. 

Our fundamental lending risk logic is deterministic based on climate exposure:
1. Low Climate Risk (High Credit Score) = Eligible for a HIGHER micro-loan disbursement tier.
2. High Climate Risk (Low Credit Score) = Restricted to a LOWER micro-loan ceiling, UNLESS they use the loan to acquire verified mitigating green technology (e.g., a solar irrigation pump), which dynamically offsets their risk and unlocks a higher loan tier.

### Core Technical Architecture Requirements (Flask Implementation)

1. INPUT DATA INTERFACE (Flask Route):
   - Create a POST `/api/v1/score-and-disburse` route that accepts and validates a JSON payload containing:
     - `farmer_id` (str)
     - `latitude` (float) and `longitude` (float)
     - `requested_loan_amount` (float)
     - `requested_asset_id` (str, optional - e.g., 'ASSET-SOLAR-PUMP-04')
     - `monthly_coop_revenue_ksh` (float)

2. GEOSPATIAL CLIMATE RISK ENGINE (Google Earth Engine Integration):
   - Use the `earthengine-api` Python library (`ee`) to extract data for the incoming GPS coordinates:
     - CHIRPS (UCSB-CHG/CHIRPS/DAILY): Load a 30-year historical baseline (1981-2011) to calculate the historical mean and standard deviation of precipitation. Fetch the trailing 12-month mean to compute a Standardized Precipitation Anomaly. An anomaly < -1.5 signifies high drought exposure.
     - MODIS NDVI (MODIS/061/MOD13Q1): Fetch a 5-year seasonal time-series. Calculate the variance of vegetation greenness during historic dry months to establish localized water-retention capability.
   - Aggregate these two metrics into a normalized Baseline Environmental Risk Score (0 to 300 points).

3. ALTERNATIVE CREDIT ENGINE:
   - Calculate a Financial Capacity Score (0 to 350 points) based on the `monthly_coop_revenue_ksh` and baseline transaction velocity variables.

4. ASSET MITIGATION ENGINE:
   - Implement an internal lookup matrix where if a high-risk farmer requests a specific asset (e.g., a solar-powered irrigation pump), the engine applies an environmental risk buffer offset (adds up to +150 points), recognizing that the hardware actively decouples the farm from rainfall dependency.

5. RISK-BASED DISBURSEMENT CALCULATION LOGIC:
   - Combine the layers into a final Climate Credit Score (300 to 850).
   - Dynamically compute the maximum allowable loan amount based on the score tier:
     - Score >= 700 (Low Risk): Max Loan Limit = 150,000 KSh.
     - Score 550 - 699 (Moderate Risk): Max Loan Limit = 75,000 KSh.
     - Score < 550 (High Risk): Max Loan Limit = 25,000 KSh (Unless an asset buffer upgrades their tier).
   - Compare the incoming `requested_loan_amount` against the dynamically generated limit.

6. B2B DISBURSEMENT OUTPUT:
   - If approved, generate a mock secure transaction payload representing an automated enterprise B2B merchant payment directly to a green tech vendor wallet, completely bypassing the consumer's wallet to avoid cash diversion.

### Deliverables Required
- Complete, organized Flask application structure (`app.py`, `scoring_engine.py`).
- Clear code comments detailing the mathematical integration of the CHIRPS anomaly and NDVI trend lines.
- Explicit try/except blocks to handle `ee.EEException` safely.
- A functional mock fallback mechanism inside the code so that if GEE credentials aren't initialized local testing won't crash during a tight hackathon environment.
- Implementation logic for a visual dashboard (e.g., Streamlit or a Flask-served HTML page) that streams incoming requests, displays the CHIRPS anomalies, maps the coordinates, and visually updates the loan approval limit changes when an asset is selected.