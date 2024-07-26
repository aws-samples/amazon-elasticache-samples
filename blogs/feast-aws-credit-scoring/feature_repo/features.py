# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from datetime import timedelta
from feast import (Entity, Feature, FeatureView, RedshiftSource,
                   ValueType)

zipcode = Entity(name="zipcode", value_type=ValueType.INT64)

zipcode_source = RedshiftSource(
    query="SELECT * FROM spectrum.zipcode_features",
    event_timestamp_column="event_timestamp",
    created_timestamp_column="created_timestamp",
)

zipcode_features = FeatureView(
    name="zipcode_features",
    entities=["zipcode"],
    ttl=timedelta(days=3650),
    features=[
        Feature(name="city", dtype=ValueType.STRING),
        Feature(name="state", dtype=ValueType.STRING),
        Feature(name="location_type", dtype=ValueType.STRING),
        Feature(name="tax_returns_filed", dtype=ValueType.INT64),
        Feature(name="population", dtype=ValueType.INT64),
        Feature(name="total_wages", dtype=ValueType.INT64),
    ],
    batch_source=zipcode_source,
)

dob_ssn = Entity(
    name="dob_ssn",
    value_type=ValueType.STRING,
    description="Date of birth and last four digits of social security number",
)

credit_history_source = RedshiftSource(
    query="SELECT * FROM spectrum.credit_history",
    event_timestamp_column="event_timestamp",
    created_timestamp_column="created_timestamp",
)

credit_history = FeatureView(
    name="credit_history",
    entities=["dob_ssn"],
    ttl=timedelta(days=90),
    features=[
        Feature(name="credit_card_due", dtype=ValueType.INT64),
        Feature(name="mortgage_due", dtype=ValueType.INT64),
        Feature(name="student_loan_due", dtype=ValueType.INT64),
        Feature(name="vehicle_loan_due", dtype=ValueType.INT64),
        Feature(name="hard_pulls", dtype=ValueType.INT64),
        Feature(name="missed_payments_2y", dtype=ValueType.INT64),
        Feature(name="missed_payments_1y", dtype=ValueType.INT64),
        Feature(name="missed_payments_6m", dtype=ValueType.INT64),
        Feature(name="bankruptcies", dtype=ValueType.INT64),
    ],
    batch_source=credit_history_source,
)
