from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path

import joblib
import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
MODEL_PATH = BASE_DIR / "models" / "best_model.pkl"
SCHEMA_PATH = BASE_DIR / "models" / "schema.json"
INPUT_FEATURES_PATH = BASE_DIR / "models" / "input_features.json"

INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
BATCH_CACHE_DIR = INSTANCE_DIR / "batch_cache"
BATCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# --- ML artifacts + feature configuration ---


def load_model_artifacts():
    with open(SCHEMA_PATH, encoding="utf-8") as schema_file:
        schema = json.load(schema_file)
    with open(INPUT_FEATURES_PATH, encoding="utf-8") as features_file:
        loaded_features = json.load(features_file)
    loaded_model = joblib.load(MODEL_PATH)
    return loaded_model, schema, loaded_features


model, model_schema, input_features = load_model_artifacts()
feature_types = model_schema["input_schema"]["dtypes"]
category_maps = model_schema.get("category_maps", {})
MODEL_SCHEMA_CHECKSUM = hashlib.sha256(
    json.dumps(model_schema, sort_keys=True).encode("utf-8")
).hexdigest()

reverse_category_maps = {
    feature: {value: label for label, value in mapping.items()}
    for feature, mapping in category_maps.items()
}

DERIVED_FEATURES = {
    "Avg Charges per Month",
    "Has Long Tenure",
    "IsMonthToMonth",
    "Payment Method Simplified",
}

BOOLEAN_FEATURES = {
    "Has Long Tenure",
    "IsMonthToMonth",
}

PAYMENT_FAMILY_ALIASES = {
    "electronic check": "Electronic",
    "mailed check": "Mailed",
    "credit card": "Card",
    "bank transfer": "Bank",
    "automatic": "Automatic",
}

BASE_FEATURES = [feature for feature in input_features if feature not in DERIVED_FEATURES]

BATCH_RESULT_FEATURES = [
    "Contract",
    "Tenure Months",
    "Monthly Charges",
    "Payment Method",
    "Internet Service",
]

payment_simplified_raw_map = category_maps.get("Payment Method Simplified", {})
PAYMENT_FAMILY_TO_MODEL_VALUE = {
    "Electronic": payment_simplified_raw_map.get("Electronic check", 1),
    "Mailed": payment_simplified_raw_map.get("Mailed check", 0),
    "Card": payment_simplified_raw_map.get("Automatic Payment", 2),
    "Bank": payment_simplified_raw_map.get("Automatic Payment", 2),
    "Other": payment_simplified_raw_map.get("Automatic Payment", 2),
}

INDIVIDUAL_SELECT_OPTIONS = {
    "Gender": ["Male", "Female"],
    "Senior Citizen": ["Yes", "No"],
    "Partner": ["Yes", "No"],
    "Dependents": ["Yes", "No"],
    "Phone Service": ["Yes", "No"],
    "Multiple Lines": ["Yes", "No", "No phone service"],
    "Internet Service": ["DSL", "Fiber optic", "No"],
    "Online Security": ["Yes", "No", "No internet service"],
    "Online Backup": ["Yes", "No", "No internet service"],
    "Device Protection": ["Yes", "No", "No internet service"],
    "Tech Support": ["Yes", "No", "No internet service"],
    "Streaming TV": ["Yes", "No", "No internet service"],
    "Streaming Movies": ["Yes", "No", "No internet service"],
    "Contract": ["Month-to-month", "One year", "Two year"],
    "Paperless Billing": ["Yes", "No"],
    "Payment Method": [
        "Electronic check",
        "Mailed check",
        "Bank transfer (automatic)",
        "Credit card (automatic)",
    ],
}

INDIVIDUAL_NUMERIC_RULES = {
    "Tenure Months": {"min": 0, "strictly_positive": True},
    "Monthly Charges": {"min": 0},
    "Total Charges": {"min": 0},
}

INDIVIDUAL_SUMMARY_FIELDS = [
    ("Genre", "Gender"),
    ("Senior", "Senior Citizen"),
    ("Contrat", "Contract"),
    ("Internet", "Internet Service"),
    ("Paiement", "Payment Method"),
    ("Charges mensuelles", "Monthly Charges"),
    ("Charges totales", "Total Charges"),
    ("Tenure (mois)", "Tenure Months"),
]


def build_model_metadata(source: str) -> dict:
    return {
        "model_path": model_schema.get("model_path") or str(MODEL_PATH),
        "schema_checksum": MODEL_SCHEMA_CHECKSUM,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "source": source,
    }


def compute_input_hash(input_payload: dict, normalized_payload: dict) -> str:
    canonical = json.dumps(
        {"input": input_payload, "normalized": normalized_payload},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def classify_risk(probability_pct: float) -> dict:
    pct = max(0.0, min(float(probability_pct), 100.0))
    if pct > 60.0:
        return {
            "risk_level": "high",
            "risk_label": "Risque élevé",
            "risk_message": "Ce client présente un risque élevé de résiliation. Une action proactive est recommandée.",
            "risk_badge_class": "high",
            "risk_pointer": pct,
        }
    if pct >= 30.0:
        return {
            "risk_level": "medium",
            "risk_label": "Risque modéré",
            "risk_message": "Risque modéré. Surveillez l'évolution et proposez des avantages pertinents.",
            "risk_badge_class": "medium",
            "risk_pointer": pct,
        }
    return {
        "risk_level": "low",
        "risk_label": "Risque faible",
        "risk_message": "Faible probabilité de churn. Concentrez-vous sur la fidélisation standard.",
        "risk_badge_class": "low",
        "risk_pointer": pct,
    }


def format_probability(probability: float) -> float:
    return round(float(probability), 4)


def get_long_tenure_threshold() -> int:
    raw = os.environ.get("LONG_TENURE_THRESHOLD", "24")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 24
    return value if value > 0 else 24


def parse_numeric_value(
    working_record: dict,
    feature: str,
    cache: dict,
    integer: bool = False,
    allow_null: bool = False,
):
    if feature in cache:
        return cache[feature]

    raw_value = working_record.get(feature)
    if raw_value in (None, "", "NULL", "null"):
        if allow_null:
            cache[feature] = None
            return None
        raise ValueError(f"Le champ {feature} ne peut pas être vide.")

    if isinstance(raw_value, (int, float)):
        numeric_value = float(raw_value)
    else:
        text_value = str(raw_value).strip().replace(",", ".")
        if text_value == "":
            if allow_null:
                cache[feature] = None
                return None
            raise ValueError(f"Le champ {feature} ne peut pas être vide.")
        try:
            numeric_value = float(text_value)
        except ValueError as exc:
            raise ValueError(f"Valeur numérique invalide pour {feature}.") from exc

    if integer:
        numeric_value = int(round(numeric_value))

    cache[feature] = numeric_value
    return numeric_value


def simplify_payment_method(payment_method_raw: str) -> str:
    if not payment_method_raw:
        return "Other"

    candidate = str(payment_method_raw).strip().lower()
    if candidate in PAYMENT_FAMILY_ALIASES:
        family = PAYMENT_FAMILY_ALIASES[candidate]
        if family != "Automatic":
            return family
    for match, family in PAYMENT_FAMILY_ALIASES.items():
        if match in candidate:
            if family == "Automatic":
                if "card" in candidate:
                    return "Card"
                if "bank" in candidate or "transfer" in candidate:
                    return "Bank"
            if family != "Automatic":
                return family

    if "card" in candidate:
        return "Card"
    if "bank" in candidate or "transfer" in candidate:
        return "Bank"
    if "elect" in candidate:
        return "Electronic"
    if "mail" in candidate:
        return "Mailed"
    if "auto" in candidate:
        return "Card"
    return "Other"


def compute_derived_values(record: dict, cache: dict) -> dict:
    derived = {}
    try:
        tenure_months = parse_numeric_value(
            record, "Tenure Months", cache, integer=True, allow_null=False
        )
    except ValueError:
        tenure_months = None

    try:
        total_charges = parse_numeric_value(
            record, "Total Charges", cache, integer=False, allow_null=False
        )
    except ValueError:
        total_charges = None

    if tenure_months and tenure_months > 0 and total_charges is not None:
        derived["Avg Charges per Month"] = round(total_charges / tenure_months, 2)
    else:
        derived["Avg Charges per Month"] = None

    threshold = get_long_tenure_threshold()
    derived["Has Long Tenure"] = bool(tenure_months and tenure_months >= threshold)

    contract_value = record.get("Contract")
    derived["IsMonthToMonth"] = str(contract_value).strip().lower() == "month-to-month"

    payment_method = record.get("Payment Method")
    derived["Payment Method Simplified"] = simplify_payment_method(payment_method)

    return derived


def extract_history_payload(record: dict) -> dict:
    return {feature: record.get(feature) for feature in input_features}


def normalize_record(raw_record: dict) -> tuple[dict, dict]:
    working_record = {feature: raw_record.get(feature) for feature in BASE_FEATURES}
    parse_cache: dict[str, float] = {}

    derived_values = compute_derived_values(working_record, parse_cache)
    working_record.update(derived_values)

    normalized: dict[str, float] = {}
    for feature in input_features:
        if feature not in working_record:
            raise ValueError(f"Le champ {feature} est manquant.")

        value = working_record[feature]

        if feature in category_maps and feature != "Payment Method Simplified":
            mapping = category_maps[feature]
            if value not in mapping:
                valid_values = ", ".join(mapping.keys())
                raise ValueError(
                    f"Valeur invalide pour {feature}. Valeurs autorisées : {valid_values}."
                )
            normalized[feature] = mapping[value]
            continue

        if feature == "Payment Method Simplified":
            mapped_value = PAYMENT_FAMILY_TO_MODEL_VALUE.get(value)
            if mapped_value is None:
                mapped_value = PAYMENT_FAMILY_TO_MODEL_VALUE.get("Other", 0)
            normalized[feature] = mapped_value
            continue

        if feature in BOOLEAN_FEATURES:
            normalized[feature] = 1 if bool(value) else 0
            continue

        dtype = feature_types.get(feature, "float64")
        if value in (None, ""):
            if dtype.startswith("float"):
                normalized[feature] = 0.0
                continue
            raise ValueError(f"Le champ {feature} ne peut pas être vide.")

        numeric_value = parse_numeric_value(
            working_record,
            feature,
            parse_cache,
            integer=dtype.startswith("int"),
            allow_null=dtype.startswith("float"),
        )
        if numeric_value is None:
            numeric_value = 0.0

        if dtype.startswith("int"):
            normalized[feature] = int(numeric_value)
        else:
            normalized[feature] = float(numeric_value)

    return normalized, working_record


def format_feature_value(feature: str, value):
    if value in (None, "", "null", "NULL"):
        return "—"
    if feature in BOOLEAN_FEATURES:
        return "Oui" if bool(value) else "Non"
    if feature == "Avg Charges per Month":
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return value
    if feature in {"Monthly Charges", "Total Charges"}:
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return value
    if feature == "Tenure Months":
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return value
    return value


def run_prediction(prepared_rows):
    if not isinstance(prepared_rows, (list, tuple)):
        raise ValueError("Les données préparées doivent être une liste de lignes.")
    if not prepared_rows:
        raise ValueError("Aucune donnée fournie pour la prédiction.")
    df = pd.DataFrame(prepared_rows, columns=input_features)
    for feature, dtype in feature_types.items():
        if dtype.startswith("int"):
            df[feature] = df[feature].astype("int64")
        else:
            df[feature] = df[feature].astype("float64")

    probabilities_raw = model.predict_proba(df)
    probabilities = probabilities_raw.tolist() if hasattr(probabilities_raw, "tolist") else list(probabilities_raw)
    decisions_raw = model.predict(df)
    decisions = decisions_raw.tolist() if hasattr(decisions_raw, "tolist") else list(decisions_raw)
    churn_probabilities = [float(row[1] if isinstance(row, (list, tuple)) else row) for row in probabilities]
    churn_decisions = [int(value) for value in decisions]
    return churn_probabilities, churn_decisions


# --- Batch cache helpers (token -> persisted JSON payload) ---


def get_batch_cache_path(token: str) -> Path:
    return BATCH_CACHE_DIR / f"{token}.json"


def save_batch_cache(token: str, payload: dict) -> None:
    path = get_batch_cache_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


def load_batch_cache(token: str) -> dict | None:
    path = get_batch_cache_path(token)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def delete_batch_cache(token: str) -> None:
    path = get_batch_cache_path(token)
    if path.exists():
        path.unlink(missing_ok=True)


def clean_batch_cache(max_age_hours: int = 12) -> None:
    threshold = datetime.utcnow() - timedelta(hours=max_age_hours)
    for path in BATCH_CACHE_DIR.glob("*.json"):
        try:
            created = datetime.utcfromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if created < threshold:
            path.unlink(missing_ok=True)


def get_batch_errors_path(token: str) -> Path:
    return BATCH_CACHE_DIR / f"{token}_errors.csv"


def build_invalid_rows_csv(invalid_rows: list[dict], columns: list[str]) -> BytesIO:
    text_buffer = StringIO()
    fieldnames = list(columns) + ["error"]
    writer = csv.DictWriter(text_buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in invalid_rows:
        record = {key: row["values"].get(key, "") for key in columns}
        record["error"] = row.get("error", "")
        writer.writerow(record)
    data = text_buffer.getvalue().encode("utf-8-sig")
    binary_buffer = BytesIO(data)
    binary_buffer.seek(0)
    return binary_buffer


def persist_invalid_rows_csv(token: str, cache: dict) -> None:
    path = get_batch_errors_path(token)
    invalid_rows = cache.get("invalid_rows", [])
    if not invalid_rows:
        if path.exists():
            path.unlink(missing_ok=True)
        return
    columns = cache.get("original_columns") or []
    buffer = build_invalid_rows_csv(invalid_rows, columns)
    with path.open("wb") as handle:
        handle.write(buffer.getvalue())

