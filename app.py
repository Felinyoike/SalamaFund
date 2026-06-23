from flask import Flask, render_template, request, redirect, url_for
import random
from climate_risk import calculate_climate_risk

app = Flask(__name__)

def calculate_combined_score(credit_score, climate_risk_score):
    """
    Scoring Rule: 
    - Credit Score is 0 to 100 (Higher is safer financially)
    - Climate Risk Score is 0 to 100 (Higher is more dangerous environmentally)
    
    Rule formula: We penalize the financial credit score based on the severity of climate risk.
    Combined Score = Credit Score * (1 - (Climate Risk Score / 150))
    """
    # Climate risk acts as a discount modifier on financial strength
    penalty_factor = 1.0 - (climate_risk_score / 150.0) 
    combined = float(credit_score) * penalty_factor
    
    # Keep score between 0 and 100
    return max(0, min(100, int(combined)))

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        # Retrieve form parameters
        farmer_data = {
            "name": request.form.get('name'),
            "id_number": request.form.get('id_number'),
            "email": request.form.get('email'),
            "location": request.form.get('location'),
            "phone": request.form.get('phone')
        }
        
        # 1. Generate random credit score (Demo purpose: 45 to 98 out of 100)
        financial_credit_score = random.randint(45, 98)
        
        # 2. Assess climate risk based on entered location
        climate_risk_score = calculate_climate_risk(farmer_data['location'])
        
        # 3. Process combined score using our scoring matrix rule
        final_salama_score = calculate_combined_score(financial_credit_score, climate_risk_score)
        
        # Determine status classification
        if final_salama_score >= 70:
            status = "Approved - Low Risk"
        elif final_salama_score >= 45:
            status = "Review Required - Medium Risk"
        else:
            status = "Declined - High Risk Exposure"

        return render_template('index.html', 
                               result=True, 
                               farmer=farmer_data, 
                               credit=financial_credit_score, 
                               climate=climate_risk_score, 
                               final=final_salama_score,
                               status=status)
                               
    return render_template('index.html', result=False)

if __name__ == '__main__':
    # Run in debug mode for easy live updates during the hackathon
    app.run(debug=True, port=5000)