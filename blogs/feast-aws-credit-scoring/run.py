# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import pandas as pd
from credit_model import CreditScoringModel

# Get historic loan data
loans = pd.read_parquet("data/loan_table.parquet")

# Create model
model = CreditScoringModel()

# Train model (using Redshift for zipcode and credit history features)
if not model.is_model_trained():
    model.train(loans)

# Make online prediction (using DynamoDB for retrieving online features)
loan_request = {
    "zipcode": [76104],
    "dob_ssn": ["19630621_4278"],
    "person_age": [133],
    "person_income": [59000],
    "person_home_ownership": ["RENT"],
    "person_emp_length": [123.0],
    "loan_intent": ["PERSONAL"],
    "loan_amnt": [35000],
    "loan_int_rate": [16.02],
}

result = model.predict(loan_request)

if result == 0:
    print("Loan approved!")
elif result == 1:
    print("Loan rejected!")
