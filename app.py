import copy
import hashlib
import json
import math
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO, StringIO
from pathlib import Path
from uuid import uuid4

import pandas as pd
from flask import (
    Flask,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    send_file,
    url_for,
)
from urllib.parse import urlencode
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from sqlalchemy import and_, case, func, inspect, or_, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from zoneinfo import ZoneInfo

from services import (
    BASE_FEATURES,
    BATCH_RESULT_FEATURES,
    BOOLEAN_FEATURES,
    DERIVED_FEATURES,
    INDIVIDUAL_NUMERIC_RULES,
    INDIVIDUAL_SELECT_OPTIONS,
    INDIVIDUAL_SUMMARY_FIELDS,
    build_model_metadata,
    category_maps,
    classify_risk,
    clean_batch_cache,
    compute_input_hash,
    extract_history_payload,
    feature_types,
    format_feature_value,
    format_probability,
    get_batch_errors_path,
    input_features,
    load_batch_cache,
    model_schema,
    normalize_record,
    persist_invalid_rows_csv,
    run_prediction,
    save_batch_cache,
)


BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
DATA_PATH = BASE_DIR / "data" / "Telco_customer_churn.xlsx"
MAIN_STYLESHEET_PATH = BASE_DIR / "static" / "css" / "main.css"

INSTANCE_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
_secret_key = os.environ.get("SECRET_KEY")
_is_dev = (os.environ.get("FLASK_ENV", "").lower() == "development") or (
    os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes"}
)
if not _secret_key:
    if _is_dev:
        _secret_key = secrets.token_urlsafe(32)
        app.logger.warning("SECRET_KEY missing; using ephemeral dev key.")
    else:
        raise RuntimeError("SECRET_KEY environment variable is required in production.")
app.config["SECRET_KEY"] = _secret_key
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{INSTANCE_DIR / 'app.db'}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB uploads
try:
    app.config["LONG_TENURE_THRESHOLD"] = int(os.environ.get("LONG_TENURE_THRESHOLD", "24"))
except (TypeError, ValueError):
    app.config["LONG_TENURE_THRESHOLD"] = 24
app.config["ALLOW_INTRA_BATCH_DUPLICATES"] = (
    os.environ.get("ALLOW_INTRA_BATCH_DUPLICATES", "false").lower() in {"1", "true", "yes"}
)

# CSRF note: forms and JSON POST endpoints do not use CSRF tokens (no Flask-WTF).

def get_long_tenure_threshold() -> int:
    try:
        return int(app.config.get("LONG_TENURE_THRESHOLD", 24))
    except (TypeError, ValueError):
        return 24

AFRICA_CASABLANCA = ZoneInfo("Africa/Casablanca")
BATCH_CHUNK_SIZE = 500
BATCH_PREVIEW_DEFAULT_PAGE_SIZE = 25
BATCH_PREVIEW_PAGE_SIZES = [5, 10, 25, 50, 100]
BATCH_ALLOWED_EXTENSIONS = {"csv", "xlsx", "xls"}
BATCH_PAGE_ALLOWED_EXTENSIONS = {"csv", "xlsx"}
BATCH_MAX_ROWS = int(os.environ.get("BATCH_MAX_ROWS", "5000"))
BATCH_MODEL_VARIANTS = [
    {"id": "production", "label": "Modèle principal (Production)"},
]
CLIENT_REFERENCE_ALIASES = [
    "clientid",
    "client",
    "customer",
    "customerid",
    "customer_id",
    "compte",
    "subscriberid",
    "subscriber",
    "idclient",
    "id_client",
]

db = SQLAlchemy(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = "login"

ROLE_USER = "Utilisateur"
DEFAULT_ADMIN_EMAIL = os.environ.get("DEFAULT_ADMIN_EMAIL", "admin@telco.local")
DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "Admin@123")
DEFAULT_ADMIN_ORG_NAME = os.environ.get("DEFAULT_ADMIN_ORG_NAME", "TelcoAI HQ")

_analysis_cache_ttl = int(os.environ.get("ANALYSIS_CACHE_TTL", "90"))
ANALYSIS_CACHE_TTL = max(60, min(_analysis_cache_ttl, 120))
analysis_cache: dict[str, tuple[float, dict]] = {}
_telco_data_cache: pd.DataFrame | None = None


@app.after_request
def ensure_main_stylesheet(response):
    """Inject the optional main stylesheet in every HTML response if present."""
    try:
        should_inject = (
            MAIN_STYLESHEET_PATH.exists()
            and response.mimetype == "text/html"
            and not response.direct_passthrough
        )
    except Exception:
        should_inject = False
    if not should_inject:
        return response

    body = response.get_data(as_text=True)
    if "</head>" not in body or "css/main.css" in body:
        return response

    href = url_for("static", filename="css/main.css")
    link_tag = f'    <link rel="stylesheet" href="{href}">\n'
    body = body.replace("</head>", f"{link_tag}</head>", 1)
    response.set_data(body)
    return response


def get_telco_data() -> pd.DataFrame:
    """Load the Telco churn dataset once and reuse the cached DataFrame."""
    global _telco_data_cache
    if _telco_data_cache is not None:
        return _telco_data_cache

    if not DATA_PATH.exists():
        _telco_data_cache = pd.DataFrame()
        return _telco_data_cache

    try:
        _telco_data_cache = pd.read_excel(DATA_PATH)
    except Exception:
        _telco_data_cache = pd.DataFrame()
    return _telco_data_cache


def _find_column(df: pd.DataFrame, target: str) -> str | None:
    """Return the actual column name matching target (case/space insensitive)."""
    normalized_target = target.strip().lower()
    for column in df.columns:
        if str(column).strip().lower() == normalized_target:
            return column
    return None


def compute_home_stats(org_id: int | None = None) -> dict:
    """Compute KPIs based on real prediction history for the landing page."""
    base_query = PredictionRecord.query
    if org_id:
        base_query = base_query.filter_by(org_id=org_id)

    total_predictions = base_query.count()
    individual_predictions = base_query.filter(
        PredictionRecord.source_type == "individual"
    ).count()
    batch_predictions = base_query.filter(
        PredictionRecord.source_type == "batch"
    ).count()

    high_risk_predictions = base_query.filter(PredictionRecord.risk_level == "high").count()

    avg_probability = (
        base_query.with_entities(func.avg(PredictionRecord.probability)).scalar() or 0.0
    )

    unique_batches = (
        base_query.filter(
            PredictionRecord.source_type == "batch",
            PredictionRecord.batch_id.isnot(None),
        )
        .with_entities(func.count(func.distinct(PredictionRecord.batch_id)))
        .scalar()
        or 0
    )

    return {
        "stats_total_predictions": total_predictions,
        "stats_individual_predictions": individual_predictions,
        "stats_batch_predictions": batch_predictions,
        "stats_high_risk_predictions": high_risk_predictions,
        "stats_avg_probability": avg_probability * 100,
        "stats_unique_batches": unique_batches,
    }


class Organization(db.Model):
    __tablename__ = "organization"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    industry = db.Column(db.String(120))
    website = db.Column(db.String(255))
    description = db.Column(db.Text)
    contact_email = db.Column(db.String(255))
    contact_phone = db.Column(db.String(50))
    address = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    users = db.relationship("User", backref="organization", lazy=True)


class User(UserMixin, db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False)
    first_name = db.Column(db.String(120))
    last_name = db.Column(db.String(120))
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    title = db.Column(db.String(120))
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    predictions = db.relationship("PredictionRecord", backref="user", lazy=True)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class PredictionRecord(db.Model):
    __tablename__ = "prediction_record"
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    client_reference = db.Column(db.String(120))
    source_type = db.Column(db.String(30), nullable=False)
    input_data = db.Column(db.Text, nullable=False)
    probability = db.Column(db.Float, nullable=False)
    decision = db.Column(db.String(50), nullable=False, index=True)
    payment_method = db.Column(db.String(120), index=True)
    contract = db.Column(db.String(120))
    internet_service = db.Column(db.String(120))
    tenure_months = db.Column(db.Integer)
    monthly_charges = db.Column(db.Float)
    risk_level = db.Column(db.String(20), index=True)
    notes = db.Column(db.Text)
    input_hash = db.Column(db.String(64), index=True)
    batch_id = db.Column(db.String(36), index=True)
    model_metadata = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    organization = db.relationship("Organization", backref=db.backref("predictions", lazy=True))


class PredictionBatchItem(db.Model):
    __tablename__ = "prediction_batch_item"
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.String(36), index=True, nullable=False)
    record_id = db.Column(
        db.Integer,
        db.ForeignKey("prediction_record.id"),
        nullable=False,
        index=True,
    )
    row_number = db.Column(db.Integer, nullable=False)
    was_saved = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    record = db.relationship(
        "PredictionRecord",
        backref=db.backref("batch_items", lazy="dynamic"),
    )


class BatchPredictionSummary(db.Model):
    __tablename__ = "batch_prediction_summary"
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.String(36), unique=True, nullable=False, index=True)
    org_id = db.Column(db.Integer, nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    file_name = db.Column(db.String(255))
    total_rows = db.Column(db.Integer, default=0)
    valid_rows = db.Column(db.Integer, default=0)
    invalid_rows = db.Column(db.Integer, default=0)
    saved_rows = db.Column(db.Integer, default=0)
    duplicate_rows = db.Column(db.Integer, default=0)
    pct_high_risk = db.Column(db.Float, default=0.0)
    pct_medium_risk = db.Column(db.Float, default=0.0)
    pct_low_risk = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class BatchPersistenceError(Exception):
    """Raised when batch predictions cannot be persisted safely."""

EMPTY_STATE_MESSAGE = "Aucune donnée pour la période choisie"


def ensure_prediction_record_columns():
    inspector = inspect(db.engine)
    columns = {col["name"] for col in inspector.get_columns(PredictionRecord.__tablename__)}
    statements = []
    if "input_hash" not in columns:
        statements.append(
            f"ALTER TABLE {PredictionRecord.__tablename__} ADD COLUMN input_hash VARCHAR(64)"
        )
    if "model_metadata" not in columns:
        statements.append(
            f"ALTER TABLE {PredictionRecord.__tablename__} ADD COLUMN model_metadata TEXT"
        )
    if "batch_id" not in columns:
        statements.append(
            f"ALTER TABLE {PredictionRecord.__tablename__} ADD COLUMN batch_id VARCHAR(36)"
        )
    if "payment_method" not in columns:
        statements.append(
            f"ALTER TABLE {PredictionRecord.__tablename__} ADD COLUMN payment_method VARCHAR(120)"
        )
    if "contract" not in columns:
        statements.append(
            f"ALTER TABLE {PredictionRecord.__tablename__} ADD COLUMN contract VARCHAR(120)"
        )
    if "internet_service" not in columns:
        statements.append(
            f"ALTER TABLE {PredictionRecord.__tablename__} ADD COLUMN internet_service VARCHAR(120)"
        )
    if "tenure_months" not in columns:
        statements.append(
            f"ALTER TABLE {PredictionRecord.__tablename__} ADD COLUMN tenure_months INTEGER"
        )
    if "monthly_charges" not in columns:
        statements.append(
            f"ALTER TABLE {PredictionRecord.__tablename__} ADD COLUMN monthly_charges FLOAT"
        )
    if "risk_level" not in columns:
        statements.append(
            f"ALTER TABLE {PredictionRecord.__tablename__} ADD COLUMN risk_level VARCHAR(20)"
        )
    if "created_at" not in columns:
        statements.append(
            f"ALTER TABLE {PredictionRecord.__tablename__} ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
        )
    if "updated_at" not in columns:
        statements.append(
            f"ALTER TABLE {PredictionRecord.__tablename__} ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
        )
    if statements:
        with db.engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    existing_indexes = inspector.get_indexes(PredictionRecord.__tablename__)
    indexed_columns = {tuple(index["column_names"]) for index in existing_indexes}
    index_statements = []
    required_indexes = {
        ("input_hash",): f"CREATE INDEX IF NOT EXISTS ix_{PredictionRecord.__tablename__}_input_hash ON {PredictionRecord.__tablename__} (input_hash)",
        ("batch_id",): f"CREATE INDEX IF NOT EXISTS ix_{PredictionRecord.__tablename__}_batch_id ON {PredictionRecord.__tablename__} (batch_id)",
        ("created_at",): f"CREATE INDEX IF NOT EXISTS ix_{PredictionRecord.__tablename__}_created_at ON {PredictionRecord.__tablename__} (created_at)",
        ("user_id",): f"CREATE INDEX IF NOT EXISTS ix_{PredictionRecord.__tablename__}_user_id ON {PredictionRecord.__tablename__} (user_id)",
        ("org_id",): f"CREATE INDEX IF NOT EXISTS ix_{PredictionRecord.__tablename__}_org_id ON {PredictionRecord.__tablename__} (org_id)",
        ("decision",): f"CREATE INDEX IF NOT EXISTS ix_{PredictionRecord.__tablename__}_decision ON {PredictionRecord.__tablename__} (decision)",
        ("payment_method",): f"CREATE INDEX IF NOT EXISTS ix_{PredictionRecord.__tablename__}_payment_method ON {PredictionRecord.__tablename__} (payment_method)",
        ("risk_level",): f"CREATE INDEX IF NOT EXISTS ix_{PredictionRecord.__tablename__}_risk_level ON {PredictionRecord.__tablename__} (risk_level)",
    }
    for columns_tuple, statement in required_indexes.items():
        if columns_tuple not in indexed_columns:
            index_statements.append(statement)
    if index_statements:
        with db.engine.begin() as connection:
            for statement in index_statements:
                connection.execute(text(statement))


def coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "oui"}
    return False


def coerce_float(value):
    if value in (None, "", "null", "NULL"):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return None


def coerce_int(value):
    numeric = coerce_float(value)
    if numeric is None:
        return None
    try:
        return int(round(numeric))
    except (TypeError, ValueError):
        return None


def build_record_profile_fields(payload: dict, probability_value: float) -> dict:
    contract = payload.get("Contract")
    internet_service = payload.get("Internet Service")
    tenure_months = coerce_int(payload.get("Tenure Months"))
    monthly_charges = coerce_float(payload.get("Monthly Charges"))
    probability_pct = max(0.0, min(float(probability_value) * 100, 100.0))
    risk_details = classify_risk(probability_pct)
    return {
        "contract": contract,
        "internet_service": internet_service,
        "tenure_months": tenure_months,
        "monthly_charges": monthly_charges,
        "risk_level": risk_details["risk_level"],
    }

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def validate_or_abort_org_access(target_org_id: int, target_user_id: int | None = None):
    if current_user.org_id != target_org_id:
        abort(403)
    if target_user_id is not None and target_user_id != current_user.id:
        abort(403)
    return


def wants_json_response() -> bool:
    requested_format = (request.args.get("format") or "").strip().lower()
    if requested_format == "json":
        return True
    accept = request.accept_mimetypes
    if not accept:
        return False
    best = accept.best_match(["application/json", "text/html"])
    if best != "application/json":
        return False
    try:
        html_quality = accept["text/html"]
    except KeyError:
        html_quality = 0
    try:
        json_quality = accept["application/json"]
    except KeyError:
        json_quality = 0
    return json_quality >= html_quality


def serialize_datetime_for_cache(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def build_analysis_cache_key(user: User, filters: dict) -> str:
    scope = f"org:{user.org_id}"
    user_scope = f"user:{user.id}"
    payload = {
        "scope": scope,
        "user": user_scope,
        "start": serialize_datetime_for_cache(filters.get("start_date")),
        "end": serialize_datetime_for_cache(filters.get("end_date")),
        "probability_min": filters.get("probability_min"),
        "has_long_tenure": filters.get("has_long_tenure"),
        "is_month_to_month": filters.get("is_month_to_month"),
        "payment_methods": sorted(filters.get("payment_methods") or []),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def get_cached_analysis(key: str) -> dict | None:
    entry = analysis_cache.get(key)
    if not entry:
        return None
    timestamp, payload = entry
    if time.time() - timestamp > ANALYSIS_CACHE_TTL:
        analysis_cache.pop(key, None)
        return None
    return payload


def set_cached_analysis(key: str, payload: dict) -> None:
    analysis_cache[key] = (time.time(), payload)


def invalidate_analysis_cache() -> None:
    analysis_cache.clear()


def build_feature_metadata():
    metadata = []
    for feature in input_features:
        dtype = feature_types.get(feature, "float64")
        metadata.append(
            {
                "name": feature,
                "dtype": dtype,
                "options": list(category_maps.get(feature, {}).keys()),
                "computed": feature in DERIVED_FEATURES,
                "is_boolean": feature in BOOLEAN_FEATURES,
            }
        )
    return metadata


feature_metadata = build_feature_metadata()
feature_metadata_map = {item["name"]: item for item in feature_metadata}
form_feature_metadata = [item for item in feature_metadata if not item["computed"]]
derived_feature_metadata = [item for item in feature_metadata if item["computed"]]


def normalize_column_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).strip().lower())


def suggest_column_mapping(columns: list[str]) -> dict[str, str | None]:
    normalized_columns = {normalize_column_key(col): col for col in columns}
    suggestions: dict[str, str | None] = {}

    aliases = {
        "tenuremonths": {"tenure", "months", "tenuremonth", "tenure_in_months"},
        "monthlycharges": {"monthlycharge", "charges", "monthly"},
        "totalcharges": {"totalcharge", "total"},
        "paymentmethod": {"payment", "paymethod"},
    }

    for feature in BASE_FEATURES:
        key = normalize_column_key(feature)
        match = normalized_columns.get(key)
        if match:
            suggestions[feature] = match
            continue
        alias_set = aliases.get(key, set())
        matched = None
        for alias in alias_set:
            matched = normalized_columns.get(alias)
            if matched:
                break
        suggestions[feature] = matched
    return suggestions


def allowed_batch_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in BATCH_ALLOWED_EXTENSIONS


def read_batch_dataframe(file_storage) -> pd.DataFrame:
    filename = file_storage.filename or ""
    extension = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    if extension not in BATCH_ALLOWED_EXTENSIONS:
        raise ValueError("Format non supporté. Merci d'importer un fichier CSV ou Excel.")
    try:
        file_storage.stream.seek(0)
        if extension == "csv":
            df = pd.read_csv(file_storage)
        else:
            df = pd.read_excel(file_storage)
    except Exception as exc:
        raise ValueError(
            "Impossible de lire le fichier fourni. Vérifiez son format et son encodage."
        ) from exc

    if df.empty:
        raise ValueError("Le fichier importé est vide.")
    if df.shape[0] > BATCH_MAX_ROWS:
        raise ValueError(
            f"Le fichier contient {df.shape[0]} lignes ; le maximum autorisé est {BATCH_MAX_ROWS}."
        )

    df.columns = [str(column).strip() for column in df.columns]
    df = df.where(pd.notnull(df), None)
    return df


def dataframe_to_rows(df: pd.DataFrame) -> tuple[list[str], list[dict]]:
    columns = [str(column) for column in df.columns]
    records = []
    for record in df.to_dict(orient="records"):
        sanitized = {}
        has_value = False
        for column, value in record.items():
            processed = None
            if value is None:
                processed = None
            elif isinstance(value, float) and math.isnan(value):
                processed = None
            elif pd.isna(value):
                processed = None
            elif isinstance(value, str):
                trimmed = value.strip()
                if trimmed == "" or trimmed.lower() == "null":
                    processed = None
                else:
                    processed = trimmed
            elif isinstance(value, (datetime, pd.Timestamp)):
                processed = value.isoformat()
            elif isinstance(value, (int, float, bool)):
                processed = value
            else:
                processed = str(value)
            sanitized[column] = processed
            if processed not in (None, "", "NULL", "null"):
                has_value = True
        if has_value:
            records.append(sanitized)
    if not records:
        raise ValueError("Toutes les lignes sont vides après nettoyage.")
    return columns, records


def build_cache_rows(records: list[dict]) -> list[dict]:
    prepared = []
    for record in records:
        prepared.append({"row_id": str(uuid4()), "values": record})
    return prepared


def suggest_client_reference_column(columns: list[str]) -> str | None:
    normalized = {normalize_column_key(column): column for column in columns}
    for alias in CLIENT_REFERENCE_ALIASES:
        match = normalized.get(alias)
        if match:
            return match
    if "client_reference" in normalized:
        return normalized["client_reference"]
    return None


def feature_form_key(feature: str) -> str:
    return normalize_column_key(feature)


def apply_mapping_to_row(row_values: dict, mapping: dict[str, str | None]) -> dict:
    base_row = {}
    for feature in BASE_FEATURES:
        source_column = mapping.get(feature)
        if source_column:
            base_row[feature] = row_values.get(source_column)
    return base_row


def chunked(sequence, size):
    for index in range(0, len(sequence), size):
        yield sequence[index : index + size]


def timezoneize(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(AFRICA_CASABLANCA)
    return dt.astimezone(AFRICA_CASABLANCA)


def load_simple_batch_dataframe(file_storage) -> pd.DataFrame:
    filename = file_storage.filename or ""
    extension = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    if extension not in BATCH_PAGE_ALLOWED_EXTENSIONS:
        raise ValueError("Format non supporté. Veuillez uploader un fichier CSV ou Excel.")
    try:
        file_storage.stream.seek(0)
        if extension == "csv":
            df = pd.read_csv(file_storage)
        else:
            df = pd.read_excel(file_storage)
    except Exception as exc:
        raise ValueError(
            "Impossible de lire le fichier importé. Vérifiez son encodage ou sa structure."
        ) from exc
    if df.empty:
        raise ValueError("Le fichier importé est vide.")
    if df.shape[0] > BATCH_MAX_ROWS:
        raise ValueError(
            f"Le fichier contient {df.shape[0]} lignes ; le maximum autorisé est {BATCH_MAX_ROWS}."
        )
    df.columns = [str(column).strip() for column in df.columns]
    df = df.where(pd.notnull(df), None)
    return df


def prepare_batch_records(df: pd.DataFrame) -> tuple[list[dict], list[str | None], str | None]:
    columns, records = dataframe_to_rows(df)
    mapping = suggest_column_mapping(columns)
    missing = ensure_mapping_complete(mapping)
    if missing:
        raise ValueError("Le fichier ne correspond pas au schéma attendu.")
    client_reference_column = suggest_client_reference_column(columns)
    prepared_rows = []
    client_references: list[str | None] = []
    for record in records:
        normalized_record = {}
        for feature, column in mapping.items():
            normalized_record[feature] = record.get(column) if column else None
        prepared_rows.append(normalized_record)
        client_references.append(record.get(client_reference_column) if client_reference_column else None)
    return prepared_rows, client_references, client_reference_column


def compute_batch_kpis(result_rows: list[dict]) -> dict:
    total = len(result_rows)
    if total == 0:
        return {
            "total": 0,
            "pct_high": 0.0,
            "pct_medium": 0.0,
            "pct_low": 0.0,
            "avg_probability": 0.0,
        }
    high = sum(1 for row in result_rows if row["risk_level"] == "high")
    medium = sum(1 for row in result_rows if row["risk_level"] == "medium")
    low = sum(1 for row in result_rows if row["risk_level"] == "low")
    avg_probability = round(sum(row["probability_pct"] for row in result_rows) / total, 2)
    return {
        "total": total,
        "pct_high": round((high / total) * 100, 2),
        "pct_medium": round((medium / total) * 100, 2),
        "pct_low": round((low / total) * 100, 2),
        "avg_probability": avg_probability,
    }


def persist_batch_predictions_history(
    user: "User",
    normalized_rows: list[dict],
    enriched_rows: list[dict],
    probabilities: list[float],
    decisions: list[int],
    client_references: list[str | None],
    notes: str | None = None,
) -> tuple[int, int, str | None]:
    if not normalized_rows:
        return 0, 0, None
    metadata = build_model_metadata("predict-batch-page")
    metadata.setdefault("extra", {})
    metadata["extra"]["source"] = "predict_batch_page"
    metadata["extra"]["batch_size"] = len(normalized_rows)
    batch_id = str(uuid4())
    metadata["extra"]["batch_id"] = batch_id
    metadata_json = json.dumps(metadata)
    saved = 0
    duplicates = 0
    for index, normalized in enumerate(normalized_rows):
        enriched = enriched_rows[index]
        payload = extract_history_payload(enriched)
        input_hash = compute_input_hash(payload, normalized)
        existing_query = PredictionRecord.query.filter_by(
            org_id=user.org_id,
            input_hash=input_hash,
            user_id=user.id,
        )
        if existing_query.first():
            duplicates += 1
            continue
        probability_raw = float(probabilities[index])
        decision_code = int(decisions[index])
        decision_label = "churn" if decision_code == 1 else "not churn"
        profile_fields = build_record_profile_fields(payload, probability_raw)
        record = PredictionRecord(
            org_id=user.org_id,
            user_id=user.id,
            client_reference=client_references[index],
            source_type="batch",
            input_data=json.dumps(payload),
            probability=format_probability(probability_raw),
            decision=decision_label,
            payment_method=payload.get("Payment Method Simplified"),
            **profile_fields,
            notes=notes,
            input_hash=input_hash,
            batch_id=batch_id,
            model_metadata=metadata_json,
        )
        db.session.add(record)
        saved += 1
    if saved:
        db.session.commit()
    return saved, duplicates, batch_id if saved else None


def build_batch_page_context() -> dict:
    allowed_extensions = sorted(BATCH_PAGE_ALLOWED_EXTENSIONS)
    return {
        "schema_features": BASE_FEATURES,
        "table_features": BATCH_RESULT_FEATURES,
        "allowed_extensions": allowed_extensions,
        "allowed_extensions_accept": ",".join(f".{ext}" for ext in allowed_extensions),
        "max_rows": BATCH_MAX_ROWS,
        "results": [],
        "results_available": False,
        "kpis": None,
        "risk_distribution": None,
        "risk_chart_data": None,
        "customer_column": None,
        "result_filename": None,
        "history_feedback": None,
        "notes": "",
        "save_history_checked": False,
        "page_sizes": BATCH_PREVIEW_PAGE_SIZES,
        "default_page_size": BATCH_PREVIEW_DEFAULT_PAGE_SIZE,
    }


# --- Authentication & account management routes ---


@app.route("/register", methods=["GET"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            flash("Identifiants invalides. Merci de réessayer.", "error")
            return render_template("login.html")

        if not user.active:
            flash("Le compte est inactif. Contactez votre administrateur.", "error")
            return render_template("login.html")

        login_user(user)
        flash("Connexion réussie.", "success")
        return redirect(url_for("home"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Vous êtes déconnecté.", "info")
    return redirect(url_for("register"))

# --- Home & landing routes ---

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    return redirect(url_for("register"))


@app.route("/home")
@login_required
def home():
    stats = compute_home_stats(org_id=current_user.org_id)
    return render_template("home.html", **stats)


def deserialize_input_data(input_data: str) -> dict:
    try:
        return json.loads(input_data)
    except json.JSONDecodeError:
        return {}


def backfill_prediction_records():
    updated = False
    for record in PredictionRecord.query.all():
        input_payload = deserialize_input_data(record.input_data)
        if not input_payload:
            continue
        try:
            normalized, enriched = normalize_record(input_payload)
        except ValueError:
            continue
        merged = {**input_payload, **enriched}
        payload = extract_history_payload(merged)
        payload_json = json.dumps(payload)
        input_hash = compute_input_hash(payload, normalized)
        if record.input_data != payload_json:
            record.input_data = payload_json
            updated = True
        if record.input_hash != input_hash:
            record.input_hash = input_hash
            updated = True
        payment_method_value = payload.get("Payment Method Simplified")
        if payment_method_value and record.payment_method != payment_method_value:
            record.payment_method = payment_method_value
            updated = True
        if record.model_metadata is None:
            record.model_metadata = json.dumps(build_model_metadata("backfill"))
            updated = True
        try:
            probability_value = float(record.probability or 0.0)
        except (TypeError, ValueError):
            probability_value = 0.0
        profile_fields = build_record_profile_fields(payload, probability_value)
        for field, value in profile_fields.items():
            if getattr(record, field) != value:
                setattr(record, field, value)
                updated = True
    if updated:
        db.session.commit()


with app.app_context():
    clean_batch_cache()
    # Database schema is managed via migrations (see `migrations/`).


# --- Prediction routes ---


@app.route("/predict/individual", methods=["GET", "POST"])
@login_required
def predict_individual():
    context = {
        "feature_metadata": feature_metadata,
        "form_feature_metadata": form_feature_metadata,
        "derived_feature_metadata": derived_feature_metadata,
        "category_maps": category_maps,
        "form_inputs": {},
        "derived_values": {},
        "summary_data": [],
        "notes": "",
        "client_reference": "",
        "probability": None,
        "probability_pct": None,
        "churn_label": None,
        "churn_badge_class": None,
        "risk_message": None,
        "risk_level": None,
    }

    if request.method == "POST":
        client_reference = request.form.get("client_reference", "").strip()
        notes = request.form.get("notes", "").strip()
        form_inputs = {}

        for feature in BASE_FEATURES:
            value = request.form.get(feature, "")
            form_inputs[feature] = value.strip() if isinstance(value, str) else value

        errors: list[str] = []
        numeric_cache: dict[str, float] = {}

        for field, options in INDIVIDUAL_SELECT_OPTIONS.items():
            selected = form_inputs.get(field, "")
            if not selected:
                errors.append(f"Le champ « {field} » est obligatoire.")
                continue
            if selected not in options:
                errors.append(f"Valeur invalide pour « {field} » : {selected}.")

        for field, rules in INDIVIDUAL_NUMERIC_RULES.items():
            raw_value = form_inputs.get(field, "")
            if raw_value in ("", None):
                errors.append(f"Le champ « {field} » est obligatoire.")
                continue
            try:
                numeric_value = float(str(raw_value).replace(",", "."))
            except ValueError:
                errors.append(f"Valeur numérique invalide pour « {field} » : {raw_value}.")
                continue

            if rules.get("strictly_positive") and numeric_value <= 0:
                errors.append(f"« {field} » doit être supérieur à 0.")
            elif numeric_value < rules.get("min", 0):
                errors.append(f"« {field} » doit être supérieur ou égal à {rules.get('min', 0)}.")

            numeric_cache[field] = numeric_value

        monthly_value = numeric_cache.get("Monthly Charges")
        total_value = numeric_cache.get("Total Charges")
        if monthly_value is not None and total_value is not None and total_value < monthly_value:
            errors.append("Les charges totales doivent être supérieures ou égales aux charges mensuelles.")

        if errors:
            for message in errors:
                flash(message, "error")
            context.update(
                {
                    "form_inputs": form_inputs,
                    "client_reference": client_reference,
                    "notes": notes,
                }
            )
            return render_template("predict_individual.html", **context)

        try:
            normalized, enriched_inputs = normalize_record(form_inputs)
        except ValueError as exc:
            flash(str(exc), "error")
            context.update(
                {
                    "form_inputs": form_inputs,
                    "client_reference": client_reference,
                    "notes": notes,
                }
            )
            return render_template("predict_individual.html", **context)

        probabilities, decisions = run_prediction([normalized])
        raw_probability = float(probabilities[0])
        decision_code = int(decisions[0])
        decision = "churn" if decision_code == 1 else "not churn"
        derived_values = {feature: enriched_inputs.get(feature) for feature in DERIVED_FEATURES}
        payload = extract_history_payload(enriched_inputs)
        probability = format_probability(raw_probability)
        save_payload = {
            "input_data": payload,
            "normalized": normalized,
            "probability": raw_probability,
            "probability_formatted": probability,
            "decision": decision,
            "decision_code": decision_code,
            "client_reference": client_reference or None,
            "model_metadata": build_model_metadata("individual-form"),
        }

        probability_pct = round(raw_probability * 100, 2)
        risk_details = classify_risk(probability_pct)
        churn_badge_class = "high" if decision == "churn" else "low"
        summary_data = [
            {
                "label": label,
                "value": form_inputs.get(field) or "—",
            }
            for label, field in INDIVIDUAL_SUMMARY_FIELDS
        ]

        context.update(
            {
                "form_inputs": form_inputs,
                "derived_values": derived_values,
                "client_reference": client_reference,
                "notes": notes,
                "probability": probability,
                "probability_pct": probability_pct,
                "churn_label": decision,
                "churn_badge_class": churn_badge_class,
                "summary_data": summary_data,
                "save_payload": save_payload,
                **risk_details,
            }
        )
        flash("Prédiction calculée.", "success")
        return render_template("predict_individual.html", **context)

    return render_template("predict_individual.html", **context)


@app.route("/predict_batch", methods=["GET", "POST"])
@login_required
def predict_batch():
    return redirect(url_for("predict_batch_workspace"), code=302)


def parse_manual_rows(payload: str):
    if not payload:
        return []
    try:
        rows = json.loads(payload)
        if not isinstance(rows, list):
            raise ValueError
    except ValueError as exc:
        raise ValueError("Format de données invalide pour les entrées manuelles.") from exc
    return rows


def validate_batch_rows(
    rows: list[dict],
    mapping: dict[str, str | None],
    excluded_ids: set[str],
    client_reference_column: str | None = None,
):
    valid_rows = []
    invalid_rows = []
    client_reference_key = client_reference_column or "client_reference"
    for row in rows:
        row_id = row["row_id"]
        if row_id in excluded_ids or row.get("excluded"):
            continue
        base_row = apply_mapping_to_row(row["values"], mapping)
        try:
            normalized, enriched = normalize_record(base_row)
            valid_rows.append(
                {
                    "row_id": row_id,
                    "base_row": base_row,
                    "normalized": normalized,
                    "enriched": enriched,
                    "client_reference": row["values"].get(client_reference_key),
                }
            )
        except ValueError as exc:
            invalid_rows.append(
                {
                    "row_id": row_id,
                    "values": row["values"],
                    "error": str(exc),
                    "error_features": detect_error_features(str(exc)),
                }
            )
    return valid_rows, invalid_rows


def compute_batch_summary(rows: list[dict], valid_rows: list[dict], invalid_rows: list[dict], excluded_ids: set[str]):
    total = len(rows)
    excluded = len(excluded_ids)
    valid = len(valid_rows)
    invalid = len(invalid_rows)
    return {
        "total": total,
        "excluded": excluded,
        "valid": valid,
        "invalid": invalid,
        "ready": valid > 0 and invalid == 0,
    }


def compute_risk_percentages(predictions: list[dict]) -> dict[str, float]:
    counts = {"high": 0, "medium": 0, "low": 0}
    for entry in predictions or []:
        probability = entry.get("Churn Probability Raw")
        if probability is None:
            probability = entry.get("Churn Probability")
            if probability is not None:
                try:
                    probability = float(probability) / 100 if probability > 1 else float(probability)
                except (TypeError, ValueError):
                    probability = None
        try:
            probability = float(probability)
        except (TypeError, ValueError):
            probability = 0.0
        if probability >= 0.66:
            counts["high"] += 1
        elif probability >= 0.33:
            counts["medium"] += 1
        else:
            counts["low"] += 1
    total = max(sum(counts.values()), 1)
    return {
        "high": round((counts["high"] / total) * 100, 2),
        "medium": round((counts["medium"] / total) * 100, 2),
        "low": round((counts["low"] / total) * 100, 2),
    }


def upsert_batch_summary(user: User, cache: dict, predictions: list[dict], saved: int, duplicates: int, batch_id: str | None) -> None:
    if not batch_id:
        return
    summary_data = cache.get("summary") or {}
    total_rows = int(summary_data.get("total") or len(cache.get("rows") or []))
    valid_rows = int(summary_data.get("valid") or saved)
    invalid_rows = int(summary_data.get("invalid") or 0)
    file_name = cache.get("upload_filename") or f"lot_{batch_id}.csv"
    risk_percentages = compute_risk_percentages(predictions)
    summary = BatchPredictionSummary.query.filter_by(batch_id=batch_id).first()
    if not summary:
        summary = BatchPredictionSummary(batch_id=batch_id, org_id=user.org_id, user_id=user.id)
    summary.file_name = file_name
    summary.total_rows = total_rows
    summary.valid_rows = valid_rows
    summary.invalid_rows = invalid_rows
    summary.saved_rows = saved
    summary.duplicate_rows = duplicates
    summary.pct_high_risk = risk_percentages["high"]
    summary.pct_medium_risk = risk_percentages["medium"]
    summary.pct_low_risk = risk_percentages["low"]
    summary.created_at = datetime.utcnow()
    db.session.add(summary)
    db.session.commit()


def detect_error_features(message: str) -> list[str]:
    related = []
    for feature in BASE_FEATURES + list(DERIVED_FEATURES):
        if feature in message:
            related.append(feature)
    return related


def paginate_items(items: list, page: int, page_size: int):
    if page_size <= 0:
        page_size = BATCH_PREVIEW_DEFAULT_PAGE_SIZE
    total_pages = max(1, math.ceil(len(items) / page_size))
    current_page = min(max(page, 1), total_pages)
    start = (current_page - 1) * page_size
    end = start + page_size
    return items[start:end], current_page, total_pages


class SimplePagination:
    def __init__(self, items: list, page: int, per_page: int, total: int):
        self.items = items
        self.page = page
        self.per_page = max(per_page, 1)
        self.total = max(total, 0)

    @property
    def pages(self) -> int:
        if self.total == 0:
            return 0
        return math.ceil(self.total / self.per_page)

    @property
    def has_prev(self) -> bool:
        return self.page > 1 and self.total > 0

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def prev_num(self) -> int | None:
        return self.page - 1 if self.has_prev else None

    @property
    def next_num(self) -> int | None:
        return self.page + 1 if self.has_next else None

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "pages": self.pages,
            "per_page": self.per_page,
            "total": self.total,
            "has_prev": self.has_prev,
            "has_next": self.has_next,
            "prev_num": self.prev_num,
            "next_num": self.next_num,
        }


def build_preview_rows(rows: list[dict], mapping: dict[str, str | None], invalid_lookup: dict[str, dict]):
    preview = []
    for row in rows:
        row_id = row["row_id"]
        invalid_entry = invalid_lookup.get(row_id)
        preview.append(
            {
                "row_id": row_id,
                "values": row["values"],
                "excluded": row.get("excluded", False),
                "invalid": bool(invalid_entry),
                "error": invalid_entry.get("error") if invalid_entry else None,
                "error_features": invalid_entry.get("error_features") if invalid_entry else [],
                "mapping": mapping,
            }
        )
    return preview


def revalidate_batch_cache(cache: dict) -> dict:
    mapping = cache.get("mapping") or {}
    rows = cache.get("rows", [])
    excluded_ids = {row["row_id"] for row in rows if row.get("excluded")}
    client_reference_column = cache.get("client_reference_column")
    valid_rows, invalid_rows = validate_batch_rows(
        rows, mapping, excluded_ids, client_reference_column
    )
    summary = compute_batch_summary(rows, valid_rows, invalid_rows, excluded_ids)
    cache["validated_rows"] = {item["row_id"]: item for item in valid_rows}
    cache["valid_row_ids"] = [item["row_id"] for item in valid_rows]
    cache["invalid_rows"] = invalid_rows
    cache["summary"] = summary
    cache["excluded_ids"] = list(excluded_ids)
    cache.pop("predictions", None)
    return cache


def generate_batch_predictions(cache: dict) -> list[dict]:
    validated_rows = cache.get("validated_rows") or {}
    if not validated_rows:
        raise ValueError("Aucune ligne valide disponible. Vérifiez le mapping et les corrections.")
    row_order = cache.get("valid_row_ids") or list(validated_rows.keys())
    normalized_rows = []
    metadata = []
    for row_id in row_order:
        info = validated_rows.get(row_id)
        if not info:
            raise ValueError("Incohérence interne : revalidez le mapping avant la prédiction.")
        normalized_rows.append(info["normalized"])
        metadata.append({
            "row_id": row_id,
            "enriched": info["enriched"],
            "client_reference": info.get("client_reference"),
        })

    if not normalized_rows:
        raise ValueError("Aucune ligne valide disponible après vérification. Corrigez les anomalies." )

    results = []
    for index, chunk in enumerate(chunked(normalized_rows, BATCH_CHUNK_SIZE)):
        probabilities, decisions = run_prediction(chunk)
        for offset, probability in enumerate(probabilities):
            meta = metadata[index * BATCH_CHUNK_SIZE + offset]
            decision_code = int(decisions[offset])
            enriched = meta["enriched"]
            formatted_probability = format_probability(probability)
            results.append(
                {
                    "row_id": meta["row_id"],
                    "Client Reference": meta["client_reference"],
                    "Churn Probability": formatted_probability,
                    "Churn Probability Raw": float(probability),
                    "Decision": "churn" if decision_code == 1 else "not churn",
                    "Decision Code": decision_code,
                    **{feature: enriched.get(feature) for feature in input_features},
                }
            )
    return results


def parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def compute_missing_values(rows: list[dict], mapping: dict[str, str | None]) -> int:
    missing = 0
    for row in rows:
        if row.get("excluded"):
            continue
        for feature, column in mapping.items():
            if not column:
                continue
            value = row["values"].get(column)
            if value in (None, "", "NULL", "null"):
                missing += 1
    return missing


def compute_dataset_missing_cells(rows: list[dict], columns: list[str]) -> int:
    missing = 0
    if not rows or not columns:
        return 0
    for row in rows:
        if row.get("excluded"):
            continue
        values = row.get("values") or {}
        for column in columns:
            value = values.get(column)
            if value in (None, "", "NULL", "null"):
                missing += 1
    return missing


def build_dataframe_from_rows(rows):
    prepared_rows = []
    enriched_rows = []
    for row in rows:
        normalized, enriched = normalize_record(row)
        prepared_rows.append(normalized)
        enriched_rows.append(enriched)
    return prepared_rows, enriched_rows

def create_batch_export(results, file_format="csv"):
    exportable = []
    for row in results:
        cloned = {key: value for key, value in row.items() if key != "row_id"}
        raw_prob = cloned.pop("Churn Probability Raw", None)
        if raw_prob is not None:
            cloned["Churn Probability (raw)"] = raw_prob
        exportable.append(cloned)
    df = pd.DataFrame(exportable)
    if file_format == "excel":
        buffer = BytesIO()
        df.to_excel(buffer, index=False)
        buffer.seek(0)
        return buffer
    # CSV: StringIO -> encode -> BytesIO
    text = StringIO()
    df.to_csv(text, index=False)
    data = text.getvalue().encode("utf-8-sig")
    buffer = BytesIO(data)
    buffer.seek(0)
    return buffer


# --- Batch prediction routes ---


@app.route("/predict/batch")
@login_required
def predict_batch_workspace():
    return render_template(
        "predict_batch_workspace.html",
        feature_metadata=feature_metadata,
        base_features=BASE_FEATURES,
        allowed_extensions=sorted(BATCH_ALLOWED_EXTENSIONS),
        allowed_extensions_accept=",".join(f".{ext}" for ext in sorted(BATCH_ALLOWED_EXTENSIONS)),
        max_rows=BATCH_MAX_ROWS,
        model_variants=BATCH_MODEL_VARIANTS,
    )


def sanitize_mapping(mapping: dict, available_columns: list[str]) -> dict[str, str]:
    sanitized = {}
    available_set = set(available_columns)
    for feature in BASE_FEATURES:
        column = mapping.get(feature)
        if column and column in available_set:
            sanitized[feature] = column
        else:
            sanitized[feature] = ""
    return sanitized


def ensure_mapping_complete(mapping: dict[str, str]) -> list[str]:
    missing = [feature for feature, column in mapping.items() if not column]
    return missing


@app.route("/predict/batch/upload", methods=["POST"])
@login_required
def predict_batch_upload():
    upload = request.files.get("data_file")
    if not upload or upload.filename == "":
        return jsonify({"status": "error", "message": "Merci de sélectionner un fichier à importer."}), 400
    if not allowed_batch_file(upload.filename):
        allowed = ", ".join(sorted(BATCH_ALLOWED_EXTENSIONS))
        return (
            jsonify(
                {
                    "status": "error",
                    "message": f"Format de fichier non supporté. Formats autorisés : {allowed}.",
                }
            ),
            400,
        )

    original_filename = secure_filename(upload.filename) or "lot.csv"
    try:
        df = read_batch_dataframe(upload)
        columns, records = dataframe_to_rows(df)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    rows = build_cache_rows(records)
    token = uuid4().hex
    mapping = suggest_column_mapping(columns)
    client_reference_column = suggest_client_reference_column(columns)
    cache = {
        "token": token,
        "rows": rows,
        "original_columns": columns,
        "mapping": mapping,
        "client_reference_column": client_reference_column,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "org_id": current_user.org_id,
        "user_id": current_user.id,
        "upload_filename": original_filename,
    }
    session["batch_token"] = token
    missing_features = ensure_mapping_complete(mapping)
    if missing_features:
        cache["summary"] = {
            "total": len(rows),
            "excluded": 0,
            "valid": 0,
            "invalid": 0,
            "ready": False,
        }
        cache["invalid_rows"] = []
        cache["validated_rows"] = {}
        cache["valid_row_ids"] = []
    else:
        revalidate_batch_cache(cache)
        persist_invalid_rows_csv(token, cache)
    save_batch_cache(token, cache)

    summary_payload = cache.get("summary") or {}
    summary_payload["missing_cells"] = compute_dataset_missing_cells(rows, columns)

    return jsonify(
        {
            "status": "ok",
            "token": token,
            "columns": columns,
            "rows": rows,
            "mapping": mapping,
            "client_reference_column": client_reference_column,
            "summary": summary_payload,
            "invalid_rows": cache.get("invalid_rows"),
            "missing_features": missing_features,
        }
    )


@app.route("/predict/batch/state")
@login_required
def predict_batch_state():
    token = (request.args.get("token") or session.get("batch_token") or "").strip()
    include_rows = (request.args.get("include_rows") or "").strip() in {"1", "true", "yes"}
    if not token:
        return jsonify({"status": "ok", "token": None})

    cache = load_batch_cache(token)
    if not cache or cache.get("org_id") != current_user.org_id:
        return jsonify({"status": "error", "message": "Session introuvable ou non autorisée."}), 404
    if cache.get("user_id") != current_user.id:
        return jsonify({"status": "error", "message": "Accès refusé."}), 403

    columns = cache.get("original_columns") or []
    rows = cache.get("rows") or []
    mapping = cache.get("mapping") or {}
    missing_features = ensure_mapping_complete(sanitize_mapping(mapping, columns))
    summary = cache.get("summary") or {}
    missing_cells = compute_dataset_missing_cells(rows, columns)

    payload = {
        "status": "ok",
        "token": token,
        "upload_filename": cache.get("upload_filename"),
        "columns": columns,
        "column_count": len(columns),
        "row_count": len(rows),
        "missing_cells": missing_cells,
        "summary": summary,
        "missing_features": missing_features,
        "has_predictions": bool(cache.get("predictions")),
    }
    if include_rows:
        payload["rows"] = rows
    return jsonify(payload)


@app.route("/predict/batch/update", methods=["POST"])
@login_required
def predict_batch_update():
    payload = request.get_json(silent=True) or {}
    token = (payload.get("token") or session.get("batch_token") or "").strip()
    rows_payload = payload.get("rows")
    if not token:
        return jsonify({"status": "error", "message": "Session de lot invalide ou expirée."}), 400

    cache = load_batch_cache(token)
    if not cache or cache.get("org_id") != current_user.org_id:
        return jsonify({"status": "error", "message": "Session introuvable ou non autorisée."}), 404
    if cache.get("user_id") != current_user.id:
        return jsonify({"status": "error", "message": "Accès refusé."}), 403

    if not isinstance(rows_payload, list):
        return jsonify({"status": "error", "message": "Payload invalide."}), 400

    columns = cache.get("original_columns") or []
    updated_rows = []
    for entry in rows_payload:
        if not isinstance(entry, dict):
            continue
        row_id = str(entry.get("row_id") or uuid4())
        values = entry.get("values") or {}
        if not isinstance(values, dict):
            values = {}
        sanitized_values = {}
        for column in columns:
            value = values.get(column)
            if isinstance(value, str):
                trimmed = value.strip()
                sanitized_values[column] = trimmed if trimmed != "" else None
            else:
                sanitized_values[column] = value
        updated_rows.append({"row_id": row_id, "values": sanitized_values})

    cache["rows"] = updated_rows
    revalidate_batch_cache(cache)
    persist_invalid_rows_csv(token, cache)
    save_batch_cache(token, cache)

    summary = cache.get("summary") or {}
    summary["missing_cells"] = compute_dataset_missing_cells(updated_rows, columns)
    return jsonify({"status": "ok", "summary": summary})


@app.route("/dataset/editor")
@login_required
def dataset_editor():
    return render_template("dataset_editor.html")


@app.route("/predict/batch/predict", methods=["POST"])
@login_required
def predict_batch_compute():
    payload = request.get_json(silent=True) or {}
    token = (payload.get("token") or session.get("batch_token") or "").strip()
    mapping_payload = payload.get("mapping")
    rows_payload = payload.get("rows")
    client_reference_column = payload.get("client_reference_column")

    if not token:
        return jsonify({"status": "error", "message": "Session de lot invalide ou expirée."}), 400

    cache = load_batch_cache(token)
    if not cache or cache.get("org_id") != current_user.org_id:
        return jsonify({"status": "error", "message": "Session introuvable ou non autorisée."}), 404
    if cache.get("user_id") != current_user.id:
        return jsonify({"status": "error", "message": "Accès refusé pour ce lot."}), 403

    available_columns = cache.get("original_columns") or []
    if mapping_payload is None:
        mapping_payload = cache.get("mapping") or {}
    if not isinstance(mapping_payload, dict):
        mapping_payload = {}
    mapping = sanitize_mapping(mapping_payload, available_columns)
    missing_features = ensure_mapping_complete(mapping)
    if missing_features:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Le dataset ne contient pas toutes les colonnes requises.",
                    "missing_features": missing_features,
                }
            ),
            400,
        )

    if rows_payload is None:
        rows_payload = cache.get("rows") or []
    if not isinstance(rows_payload, list) or not rows_payload:
        return jsonify({"status": "error", "message": "Aucune donnée fournie pour la prédiction."}), 400

    if client_reference_column is None:
        client_reference_column = cache.get("client_reference_column")
    if client_reference_column not in available_columns:
        client_reference_column = None

    updated_rows = []
    for entry in rows_payload:
        if not isinstance(entry, dict):
            continue
        row_id = str(entry.get("row_id") or uuid4())
        values = entry.get("values") or {}
        sanitized_values = {}
        for column in available_columns:
            value = values.get(column)
            if isinstance(value, str):
                trimmed = value.strip()
                sanitized_values[column] = trimmed if trimmed != "" else None
            else:
                sanitized_values[column] = value
        updated_rows.append({"row_id": row_id, "values": sanitized_values})

    if not updated_rows:
        return jsonify({"status": "error", "message": "Aucune ligne exploitable pour la prédiction."}), 400

    cache["rows"] = updated_rows
    cache["mapping"] = mapping
    cache["client_reference_column"] = client_reference_column or None

    revalidate_batch_cache(cache)
    persist_invalid_rows_csv(token, cache)

    invalid_rows = cache.get("invalid_rows") or []
    summary = cache.get("summary") or {}
    if invalid_rows:
        save_batch_cache(token, cache)
        return (
            jsonify(
                {
                    "status": "invalid",
                    "message": "Certaines lignes sont invalides. Corrigez-les dans l’éditeur du dataset.",
                    "summary": summary,
                    "invalid_rows": invalid_rows,
                }
            ),
            422,
        )

    try:
        results = generate_batch_predictions(cache)
    except ValueError as exc:
        save_batch_cache(cache["token"], cache)
        return jsonify({"status": "error", "message": str(exc)}), 400

    metadata = build_model_metadata("batch-upload")
    cache["predictions"] = results
    cache["prediction_metadata"] = metadata
    cache["prediction_generated_at"] = datetime.utcnow().isoformat() + "Z"
    save_batch_cache(cache["token"], cache)

    return jsonify(
        {
            "status": "ok",
            "predictions": results,
            "summary": summary,
            "metadata": metadata,
            "generated_at": cache["prediction_generated_at"],
        }
    )


@app.route("/predict/batch/export", methods=["POST"])
@login_required
def predict_batch_export():
    payload = request.get_json(silent=True) or {}
    token = payload.get("token")
    file_format = (payload.get("format") or "csv").lower()
    if file_format not in {"csv", "excel"}:
        return jsonify({"status": "error", "message": "Format d'export inconnu."}), 400
    if not token:
        return jsonify({"status": "error", "message": "Session de lot manquante."}), 400

    cache = load_batch_cache(token)
    if not cache or cache.get("org_id") != current_user.org_id:
        return jsonify({"status": "error", "message": "Session introuvable ou non autorisée."}), 404
    if cache.get("user_id") != current_user.id:
        return jsonify({"status": "error", "message": "Accès refusé pour cet export."}), 403

    predictions = cache.get("predictions")
    if not predictions:
        return jsonify({"status": "error", "message": "Aucun résultat disponible pour l'export."}), 400

    buffer = create_batch_export(predictions, file_format=file_format)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    if file_format == "excel":
        filename = f"predictions_{timestamp}.xlsx"
        mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        filename = f"predictions_{timestamp}.csv"
        mimetype = "text/csv"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype=mimetype)


@app.route("/predict/batch/history", methods=["POST"])
@login_required
def predict_batch_history():
    payload = request.get_json(silent=True) or {}
    token = payload.get("token")
    notes = (payload.get("notes") or "").strip() or None
    if not token:
        return jsonify({"status": "error", "message": "Session de lot introuvable."}), 400

    cache = load_batch_cache(token)
    if not cache or cache.get("org_id") != current_user.org_id:
        return jsonify({"status": "error", "message": "Session introuvable ou non autorisée."}), 404
    if cache.get("user_id") != current_user.id:
        return jsonify({"status": "error", "message": "Accès refusé pour ce lot."}), 403

    predictions = cache.get("predictions")
    if not predictions:
        return jsonify(
            {"status": "error", "message": "Aucune prédiction calculée. Lancez d'abord le calcul."}
        ), 400

    validated_rows = cache.get("validated_rows") or {}
    if not validated_rows:
        return jsonify({"status": "error", "message": "Validation manquante. Relancez la prédiction."}), 400

    metadata = cache.get("prediction_metadata") or build_model_metadata("batch-upload")
    if cache.get("prediction_generated_at"):
        metadata["generated_at"] = cache["prediction_generated_at"]
    metadata.setdefault("extra", {})
    metadata["extra"]["batch_size"] = len(predictions)
    metadata["extra"]["client_reference_column"] = cache.get("client_reference_column")

    def build_metadata_json_for_batch(batch_identifier: str) -> str:
        snapshot = copy.deepcopy(metadata)
        snapshot.setdefault("extra", {})
        snapshot["extra"]["batch_id"] = batch_identifier
        return json.dumps(snapshot)

    prediction_lookup = {row["row_id"]: row for row in predictions}
    prepared_rows: list[dict] = []
    for row_id, details in validated_rows.items():
        prediction_row = prediction_lookup.get(row_id)
        if not prediction_row:
            continue
        normalized = details["normalized"]
        enriched = details["enriched"]
        payload_ready = extract_history_payload(enriched)
        input_hash = compute_input_hash(payload_ready, normalized)
        probability_raw = prediction_row.get("Churn Probability Raw")
        try:
            probability_value = float(probability_raw)
        except (TypeError, ValueError):
            probability_value = 0.0
        profile_fields = build_record_profile_fields(payload_ready, probability_value)

        prepared_rows.append(
            {
                "input_hash": input_hash,
                "record_kwargs": {
                    "org_id": current_user.org_id,
                    "user_id": current_user.id,
                    "client_reference": prediction_row.get("Client Reference"),
                    "source_type": "batch",
                    "input_data": json.dumps(payload_ready),
                    "probability": format_probability(probability_value),
                    "decision": prediction_row.get("Decision"),
                    "payment_method": payload_ready.get("Payment Method Simplified"),
                    "notes": notes,
                    **profile_fields,
                },
            }
        )

    if not prepared_rows:
        return jsonify(
            {
                "status": "ok",
                "saved": 0,
                "history_url": url_for("history"),
            }
        )

    candidate_batch_id = str(uuid4())
    metadata_json = build_metadata_json_for_batch(candidate_batch_id)
    saved = 0

    try:
        for prepared in prepared_rows:
            record_kwargs = prepared["record_kwargs"]
            record = PredictionRecord(
                **record_kwargs,
                input_hash=prepared.get("input_hash"),
                batch_id=candidate_batch_id,
                model_metadata=metadata_json,
            )
            db.session.add(record)
            saved += 1
        db.session.commit()
        invalidate_analysis_cache()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("batch history save failed")
        return jsonify({"status": "error", "message": "Impossible d'enregistrer ce lot."}), 400

    cache["prediction_metadata"] = metadata
    save_batch_cache(token, cache)

    return jsonify(
        {
            "status": "ok",
            "saved": saved,
            "history_url": url_for("history"),
            "batch_id": candidate_batch_id,
            "batch_size": len(prepared_rows),
        }
    )



# --- History routes ---


@app.route("/predict/batch/<token>/invalid")
@login_required
def predict_batch_invalid(token):
    cache = load_batch_cache(token)
    if not cache or cache.get("org_id") != current_user.org_id:
        abort(404)
    if cache.get("user_id") != current_user.id:
        abort(403)
    path = get_batch_errors_path(token)
    if not path.exists():
        abort(404)
    filename = f"erreurs_{token}.csv"
    return send_file(path, as_attachment=True, download_name=filename, mimetype="text/csv")


@app.route("/history")
@login_required
def history():
    page = parse_int(request.args.get("page"), 1)
    page_size = parse_int(request.args.get("page_size"), 25)
    if page_size <= 0:
        page_size = 25
    page_size = max(5, min(100, page_size))

    search_query = (request.args.get("search") or request.args.get("q") or "").strip()
    risk_filter = (request.args.get("risk") or "all").lower()
    source_filter = (request.args.get("source") or "all").lower()
    start_date_str = request.args.get("start_date", "")
    end_date_str = request.args.get("end_date", "")

    def parse_date(value: str) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None

    start_date = parse_date(start_date_str)
    end_date = parse_date(end_date_str)
    if end_date:
        end_date = end_date + timedelta(days=1)

    base_query = PredictionRecord.query.filter_by(
        org_id=current_user.org_id,
        user_id=current_user.id,
    )

    stats_row = (
        base_query.with_entities(
            func.count(PredictionRecord.id),
            func.avg(PredictionRecord.probability),
            func.sum(case((PredictionRecord.probability > 0.6, 1), else_=0)),
            func.sum(
                case(
                    (and_(PredictionRecord.probability >= 0.3, PredictionRecord.probability <= 0.6), 1),
                    else_=0,
                )
            ),
            func.sum(case((PredictionRecord.probability < 0.3, 1), else_=0)),
        ).first()
    )
    total_records = stats_row[0] if stats_row else 0
    avg_probability = stats_row[1] if stats_row else 0.0
    high_count = stats_row[2] if stats_row else 0
    medium_count = stats_row[3] if stats_row else 0
    low_count = stats_row[4] if stats_row else 0

    def percentage(value: float, total: float) -> float:
        return round((float(value) / total) * 100, 2) if total else 0.0

    stats = {
        "total": int(total_records or 0),
        "avg_probability_pct": round(float(avg_probability or 0.0) * 100, 2),
        "risk_high": int(high_count or 0),
        "risk_medium": int(medium_count or 0),
        "risk_low": int(low_count or 0),
        "pct_high": percentage(high_count, total_records),
        "pct_medium": percentage(medium_count, total_records),
        "pct_low": percentage(low_count, total_records),
    }

    query = base_query
    if search_query:
        search_like = f"%{search_query}%"
        search_filters = [PredictionRecord.client_reference.ilike(search_like)]
        if search_query.isdigit():
            search_filters.append(PredictionRecord.id == int(search_query))
        if len(search_filters) == 1:
            query = query.filter(search_filters[0])
        else:
            query = query.filter(or_(*search_filters))

    if risk_filter == "high":
        query = query.filter(PredictionRecord.probability > 0.6)
    elif risk_filter == "medium":
        query = query.filter(
            and_(PredictionRecord.probability >= 0.3, PredictionRecord.probability <= 0.6)
        )
    elif risk_filter == "low":
        query = query.filter(PredictionRecord.probability < 0.3)

    if source_filter == "individual":
        query = query.filter(PredictionRecord.source_type != "batch")
    elif source_filter == "batch":
        query = query.filter(PredictionRecord.source_type == "batch")

    if start_date:
        query = query.filter(PredictionRecord.created_at >= start_date)
    if end_date:
        query = query.filter(PredictionRecord.created_at < end_date)

    total_filtered = query.count()
    total_pages = max(1, math.ceil(total_filtered / page_size)) if total_filtered else 1
    current_page = min(max(page, 1), total_pages)
    offset = (current_page - 1) * page_size

    records = (
        query.order_by(PredictionRecord.created_at.desc())
        .offset(offset)
        .limit(page_size)
        .all()
    )

    predictions = []
    for record in records:
        probability_value = float(record.probability or 0.0)
        probability_pct = round(probability_value * 100, 2)
        risk_details = classify_risk(probability_pct)
        churn_label = record.decision or "—"
        churn_badge_class = "high" if churn_label == "churn" else "low"
        localized_timestamp = timezoneize(record.created_at)
        display_timestamp = localized_timestamp.strftime("%d/%m/%Y %H:%M")

        input_payload = deserialize_input_data(record.input_data)
        try:
            _, enriched_data = normalize_record(input_payload)
            payload = extract_history_payload({**input_payload, **enriched_data})
        except ValueError:
            payload = input_payload or {}

        detail_payload = {
            "id": record.id,
            "customer_reference": record.client_reference or "—",
            "probability_display": f"{probability_pct:.2f} %",
            "churn_label": churn_label,
            "churn_badge_class": churn_badge_class,
            "risk_label": risk_details["risk_label"],
            "risk_level": risk_details["risk_level"],
            "risk_badge_class": risk_details["risk_badge_class"],
            "source_label": "Mode batch" if record.source_type == "batch" else "Mode individuel",
            "created_at": display_timestamp,
            "notes": record.notes or "",
            "payload": payload,
        }

        predictions.append(
            {
                "id": record.id,
                "customer_reference": record.client_reference or "—",
                "probability_pct": probability_pct,
                "probability_display": f"{probability_pct:.2f} %",
                "churn_label": churn_label,
                "churn_badge_class": churn_badge_class,
                "risk_label": risk_details["risk_label"],
                "risk_badge_class": risk_details["risk_badge_class"],
                "source_type": record.source_type or "individual",
                "source_label": "Batch" if record.source_type == "batch" else "Individuelle",
                "created_display": display_timestamp,
                "detail_payload": detail_payload,
            }
        )

    pagination = SimplePagination(predictions, current_page, page_size, total_filtered)

    def build_page_url(target_page: int) -> str:
        params = request.args.to_dict()
        params["page"] = target_page
        params["page_size"] = page_size
        return url_for("history", **params)

    pages_count = pagination.pages or 1
    if pages_count <= 7:
        page_numbers = list(range(1, pages_count + 1))
    else:
        start = max(1, pagination.page - 2)
        end = min(pages_count, pagination.page + 2)
        if start == 1:
            end = min(pages_count, 5)
        elif end == pages_count:
            start = max(1, pages_count - 4)
        page_numbers = list(range(start, end + 1))

    page_links = [
        {"number": number, "url": build_page_url(number), "active": number == pagination.page}
        for number in page_numbers
    ]
    pagination_links = {
        "prev": build_page_url(pagination.prev_num) if pagination.has_prev else None,
        "next": build_page_url(pagination.next_num) if pagination.has_next else None,
    }

    filters = {
        "search": search_query,
        "risk": risk_filter,
        "source": source_filter,
        "start_date": start_date_str,
        "end_date": end_date_str,
        "page_size": page_size,
    }

    risk_chart_data = {
        "labels": ["Risque élevé", "Risque moyen", "Risque faible"],
        "values": [stats["pct_high"], stats["pct_medium"], stats["pct_low"]],
    }

    current_full_path = request.full_path.rstrip("?") or url_for("history")

    if wants_json_response():
        return jsonify(
            {
                "status": "ok",
                "records": predictions,
                "stats": stats,
                "filters": filters,
                "pagination": pagination.to_dict(),
                "result_count": total_filtered,
            }
        )

    return render_template(
        "history.html",
        predictions=predictions,
        stats=stats,
        filters=filters,
        pagination=pagination,
        page_links=page_links,
        pagination_links=pagination_links,
        result_count=total_filtered,
        risk_chart_data=risk_chart_data,
        request_path=current_full_path,
    )

@app.route("/history/<int:record_id>/edit", methods=["GET", "POST"])
@login_required
def edit_history(record_id):
    record = PredictionRecord.query.get_or_404(record_id)
    validate_or_abort_org_access(record.org_id, record.user_id)

    if request.method == "POST":
        client_reference = request.form.get("client_reference", "").strip() or None
        notes = request.form.get("notes", "").strip() or None

        form_inputs: dict[str, str | int | float | None] = {}
        for feature in BASE_FEATURES:
            value = request.form.get(feature, "")
            form_inputs[feature] = value.strip() if isinstance(value, str) else value

        errors: list[str] = []
        numeric_cache: dict[str, float] = {}

        for field, options in INDIVIDUAL_SELECT_OPTIONS.items():
            selected = form_inputs.get(field, "")
            if not selected:
                errors.append(f"Le champ « {field} » est obligatoire.")
                continue
            if selected not in options:
                errors.append(f"Valeur invalide pour « {field} » : {selected}.")

        for field, rules in INDIVIDUAL_NUMERIC_RULES.items():
            raw_value = form_inputs.get(field, "")
            if raw_value in ("", None):
                errors.append(f"Le champ « {field} » est obligatoire.")
                continue
            try:
                numeric_value = float(str(raw_value).replace(",", "."))
            except ValueError:
                errors.append(f"Valeur numérique invalide pour « {field} » : {raw_value}.")
                continue

            if rules.get("strictly_positive") and numeric_value <= 0:
                errors.append(f"« {field} » doit être supérieur à 0.")
            elif numeric_value < rules.get("min", 0):
                errors.append(f"« {field} » doit être supérieur ou égal à {rules.get('min', 0)}.")

            numeric_cache[field] = numeric_value

        monthly_value = numeric_cache.get("Monthly Charges")
        total_value = numeric_cache.get("Total Charges")
        if monthly_value is not None and total_value is not None and total_value < monthly_value:
            errors.append("Les charges totales doivent être supérieures ou égales aux charges mensuelles.")

        if errors:
            for message in errors:
                flash(message, "error")
            input_data = deserialize_input_data(record.input_data)
            payload = extract_history_payload(input_data) if input_data else form_inputs
            return render_template(
                "history_edit.html",
                record=record,
                input_data=payload,
                feature_metadata=feature_metadata,
                client_reference=client_reference or record.client_reference,
                notes=notes or record.notes,
            )

        try:
            normalized, enriched_inputs = normalize_record(form_inputs)
        except ValueError as exc:
            flash(str(exc), "error")
            payload = extract_history_payload(form_inputs)
            return render_template(
                "history_edit.html",
                record=record,
                input_data=payload,
                feature_metadata=feature_metadata,
                client_reference=client_reference or record.client_reference,
                notes=notes or record.notes,
            )

        probabilities, decisions = run_prediction([normalized])
        raw_probability = float(probabilities[0])
        decision_code = int(decisions[0])
        decision = "churn" if decision_code == 1 else "not churn"

        payload_ready = extract_history_payload(enriched_inputs)
        input_hash = compute_input_hash(payload_ready, normalized)
        profile_fields = build_record_profile_fields(payload_ready, raw_probability)
        metadata = build_model_metadata("history-edit")

        record.client_reference = client_reference
        record.notes = notes
        record.input_data = json.dumps(payload_ready)
        record.probability = format_probability(raw_probability)
        record.decision = decision
        record.input_hash = input_hash
        record.model_metadata = json.dumps(metadata)
        record.payment_method = payload_ready.get("Payment Method Simplified")
        for field, value in profile_fields.items():
            setattr(record, field, value)

        db.session.commit()
        invalidate_analysis_cache()
        flash("Client mis à jour et re-prédit.", "success")
        return redirect(url_for("history"))

    input_data = deserialize_input_data(record.input_data)
    try:
        _, enriched_data = normalize_record(input_data)
    except ValueError:
        enriched_data = input_data
    payload = extract_history_payload({**input_data, **enriched_data})
    return render_template(
        "history_edit.html",
        record=record,
        input_data=payload,
        feature_metadata=feature_metadata,
        client_reference=record.client_reference,
        notes=record.notes,
    )


@app.route("/history/<int:record_id>/delete", methods=["POST"])
@login_required
def delete_history(record_id):
    record = PredictionRecord.query.get_or_404(record_id)
    validate_or_abort_org_access(record.org_id, record.user_id)
    db.session.delete(record)
    db.session.commit()
    invalidate_analysis_cache()
    flash("Entrée supprimée.", "success")
    redirect_to = request.form.get("redirect_to", "")
    if redirect_to and redirect_to.startswith("/"):
        return redirect(redirect_to)
    return redirect(url_for("history"))


@app.route("/history/save", methods=["POST"])
@login_required
def history_save():
    payload_wrapper = request.get_json(silent=True) or {}
    payload = payload_wrapper.get("payload") or {}
    input_data = payload.get("input_data")
    client_reference = payload.get("client_reference")
    notes = (payload_wrapper.get("notes") or "").strip() or None
    decision = payload.get("decision")
    try:
        decision_code = int(payload.get("decision_code"))
    except (TypeError, ValueError):
        decision_code = None
    probability_raw = payload.get("probability")

    if not input_data or probability_raw is None or decision is None:
        return (
            jsonify({"status": "error", "message": "Payload incomplet pour l'enregistrement."}),
            400,
        )

    try:
        normalized, enriched = normalize_record(input_data)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    payload_ready = extract_history_payload(enriched)
    input_hash = compute_input_hash(payload_ready, normalized)

    try:
        probability_value = float(probability_raw)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Probabilité invalide."}), 400

    metadata_in = payload.get("model_metadata") or {}
    metadata = build_model_metadata(metadata_in.get("source", "individual-form"))
    if metadata_in.get("generated_at"):
        metadata["generated_at"] = metadata_in["generated_at"]
    if metadata_in.get("extra"):
        metadata["extra"] = metadata_in["extra"]

    decision_label = "churn" if decision_code == 1 else "not churn"
    profile_fields = build_record_profile_fields(payload_ready, probability_value)

    record = PredictionRecord(
        org_id=current_user.org_id,
        user_id=current_user.id,
        client_reference=(client_reference or payload_wrapper.get("client_reference") or None),
        source_type="individual",
        input_data=json.dumps(payload_ready),
        probability=format_probability(probability_value),
        decision=decision_label,
        payment_method=payload_ready.get("Payment Method Simplified"),
        **profile_fields,
        notes=notes,
        input_hash=input_hash,
        model_metadata=json.dumps(metadata),
    )
    db.session.add(record)
    db.session.commit()
    invalidate_analysis_cache()

    return jsonify({"status": "ok", "history_url": url_for("history")})


@app.route("/history/export")
@login_required
def history_export():
    records = (
        PredictionRecord.query.filter_by(org_id=current_user.org_id, user_id=current_user.id)
        .order_by(PredictionRecord.created_at.asc())
        .all()
    )
    export_rows: list[dict] = []
    all_features = list(BASE_FEATURES) + list(DERIVED_FEATURES)
    for record in records:
        payload = deserialize_input_data(record.input_data)
        row = {feature: payload.get(feature) for feature in all_features}
        row.update(
            {
                "Record ID": record.id,
                "Client Reference": record.client_reference,
                "Source": record.source_type,
                "Predicted At": timezoneize(record.created_at).isoformat(),
                "Churn Probability": round(float(record.probability or 0.0) * 100, 2),
                "Churn Label": record.decision,
                "Notes": record.notes,
                "Batch ID": record.batch_id,
            }
        )
        export_rows.append(row)

    df = pd.DataFrame(export_rows)
    text = StringIO()
    df.to_csv(text, index=False)
    data = text.getvalue().encode("utf-8-sig")
    buffer = BytesIO(data)
    buffer.seek(0)
    filename = f"teleco_churn_history_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(buffer, mimetype="text/csv", as_attachment=True, download_name=filename)


# --- REST API (JSON) ---


def _api_error(message: str, status_code: int = 400):
    return jsonify({"status": "error", "message": message}), status_code


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _is_valid_email(value: str) -> bool:
    if not value:
        return False
    return bool(_EMAIL_RE.match(value.strip()))


def _is_valid_url(value: str) -> bool:
    if not value:
        return True
    try:
        from urllib.parse import urlparse

        parsed = urlparse(value.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def _validate_password_strength(password: str) -> list[str]:
    errors: list[str] = []
    if len(password) < 8:
        errors.append("Le mot de passe doit contenir au moins 8 caractères.")
    if not re.search(r"[A-Z]", password):
        errors.append("Le mot de passe doit contenir au moins une majuscule.")
    if not re.search(r"[a-z]", password):
        errors.append("Le mot de passe doit contenir au moins une minuscule.")
    if not re.search(r"\d", password):
        errors.append("Le mot de passe doit contenir au moins un chiffre.")
    return errors


@app.route("/api/orgs", methods=["POST"])
def api_orgs_create():
    if current_user.is_authenticated:
        return _api_error("Déjà authentifié.", 400)

    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    industry = (payload.get("industry") or "").strip()
    website = (payload.get("website") or "").strip()
    address = (payload.get("address") or "").strip()
    contact_email = (payload.get("contact_email") or "").strip()
    contact_phone = (payload.get("contact_phone") or "").strip()
    description = (payload.get("description") or "").strip()

    field_errors: dict[str, str] = {}
    if not name:
        field_errors["name"] = "Le nom de l'entreprise est obligatoire."
    if contact_email and not _is_valid_email(contact_email):
        field_errors["contact_email"] = "Format d'e-mail invalide."
    if website and not _is_valid_url(website):
        field_errors["website"] = "URL invalide. Utilisez http(s)://"

    if field_errors:
        return jsonify({"status": "error", "message": "Validation échouée.", "fields": field_errors}), 422

    if Organization.query.filter(func.lower(Organization.name) == name.lower()).first():
        return jsonify({"status": "error", "message": "Ce nom d’organisation existe déjà."}), 409

    organization = Organization(
        name=name,
        industry=industry or None,
        website=website or None,
        description=description or None,
        contact_email=contact_email or None,
        contact_phone=contact_phone or None,
        address=address or None,
    )
    db.session.add(organization)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"status": "error", "message": "Ce nom d’organisation existe déjà."}), 409

    session["pending_org_id"] = organization.id
    return jsonify({"status": "ok", "org": {"id": organization.id, "name": organization.name}})


@app.route("/api/users", methods=["POST"])
def api_users_create_main():
    if current_user.is_authenticated:
        return _api_error("Déjà authentifié.", 400)

    pending_org_id = session.get("pending_org_id")
    if not pending_org_id:
        return _api_error("Organisation manquante. Reprenez l'étape Organisation.", 400)

    payload = request.get_json(silent=True) or {}
    org_id = payload.get("org_id") or pending_org_id
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return _api_error("Organisation invalide.", 400)
    if org_id_int != int(pending_org_id):
        return _api_error("Organisation non autorisée pour cette inscription.", 403)

    first_name = (payload.get("first_name") or "").strip()
    last_name = (payload.get("last_name") or "").strip()
    title = (payload.get("title") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    confirm_password = payload.get("confirm_password") or ""

    field_errors: dict[str, str] = {}
    if not first_name:
        field_errors["first_name"] = "Le prénom est obligatoire."
    if not last_name:
        field_errors["last_name"] = "Le nom est obligatoire."
    if not email:
        field_errors["email"] = "L'adresse e-mail est obligatoire."
    elif not _is_valid_email(email):
        field_errors["email"] = "Format d'e-mail invalide."
    if password != confirm_password:
        field_errors["confirm_password"] = "Les mots de passe ne correspondent pas."

    password_errors = _validate_password_strength(password)
    if password_errors:
        field_errors["password"] = password_errors[0]

    if User.query.filter_by(email=email).first():
        field_errors["email"] = "Un compte existe déjà avec cette adresse e-mail."

    if field_errors:
        return jsonify({"status": "error", "message": "Validation échouée.", "fields": field_errors}), 422

    organization = Organization.query.get(org_id_int)
    if not organization:
        return _api_error("Organisation introuvable.", 404)

    new_user = User(
        organization=organization,
        first_name=first_name,
        last_name=last_name,
        title=title or None,
        email=email,
    )
    new_user.set_password(password)
    db.session.add(new_user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"status": "error", "message": "Impossible de créer l'utilisateur."}), 400

    session.pop("pending_org_id", None)
    return jsonify({"status": "ok", "redirect_url": url_for("login")})


@app.route("/api/auth/signup", methods=["POST"])
def api_auth_signup():
    if current_user.is_authenticated:
        return _api_error("Déjà authentifié.", 400)
    payload = request.get_json(silent=True) or {}
    company = payload.get("company") or {}
    user_payload = payload.get("user") or {}
    if not isinstance(company, dict) or not isinstance(user_payload, dict):
        return _api_error("Payload invalide.", 400)

    org_name = (company.get("name") or "").strip()
    email = (user_payload.get("email") or "").strip().lower()
    password = user_payload.get("password") or ""
    if not org_name:
        return _api_error("Le nom de l'entreprise est obligatoire.", 422)
    if not email:
        return _api_error("L'adresse e-mail est obligatoire.", 422)
    if len(password) < 8:
        return _api_error("Le mot de passe doit contenir au moins 8 caractères.", 422)
    if User.query.filter_by(email=email).first():
        return _api_error("Un compte existe déjà avec cette adresse e-mail.", 409)
    if Organization.query.filter(func.lower(Organization.name) == org_name.lower()).first():
        return _api_error("Ce nom d’organisation existe déjà.", 409)

    organization = Organization(
        name=org_name,
        industry=(company.get("industry") or "").strip(),
        website=(company.get("website") or "").strip(),
        description=(company.get("description") or "").strip(),
        contact_email=(company.get("contact_email") or "").strip(),
        contact_phone=(company.get("contact_phone") or "").strip(),
        address=(company.get("address") or "").strip(),
    )
    new_user = User(
        organization=organization,
        first_name=(user_payload.get("first_name") or "").strip(),
        last_name=(user_payload.get("last_name") or "").strip(),
        title=(user_payload.get("title") or "").strip(),
        email=email,
    )
    new_user.set_password(password)
    db.session.add(organization)
    db.session.add(new_user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return _api_error("Impossible de créer le compte.", 400)

    login_user(new_user)
    return jsonify({"status": "ok", "redirect_url": url_for("home")})


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    if current_user.is_authenticated:
        return jsonify({"status": "ok", "redirect_url": url_for("home")})
    payload = request.get_json(silent=True) or {}
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    if not email or not password:
        return _api_error("Email et mot de passe requis.", 422)
    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return _api_error("Identifiants invalides.", 401)
    if not user.active:
        return _api_error("Compte inactif.", 403)
    login_user(user)
    return jsonify({"status": "ok", "redirect_url": url_for("home")})


@app.route("/api/auth/logout", methods=["POST"])
@login_required
def api_auth_logout():
    logout_user()
    return jsonify({"status": "ok", "redirect_url": url_for("register")})


@app.route("/api/auth/me", methods=["GET"])
@login_required
def api_auth_me():
    return jsonify(
        {
            "status": "ok",
            "user": {
                "id": current_user.id,
                "org_id": current_user.org_id,
                "email": current_user.email,
                "first_name": current_user.first_name,
                "last_name": current_user.last_name,
                "title": current_user.title,
            },
        }
    )


@app.route("/api/predict/individual", methods=["POST"])
@login_required
def api_predict_individual():
    payload = request.get_json(silent=True) or {}
    client = payload.get("client") if isinstance(payload.get("client"), dict) else payload
    if not isinstance(client, dict):
        return _api_error("Payload invalide.", 400)

    form_inputs = {}
    for feature in BASE_FEATURES:
        form_inputs[feature] = client.get(feature)

    try:
        normalized, enriched_inputs = normalize_record(form_inputs)
    except ValueError as exc:
        return _api_error(str(exc), 422)

    probabilities, decisions = run_prediction([normalized])
    raw_probability = float(probabilities[0])
    decision_code = int(decisions[0])
    churn_label = "churn" if decision_code == 1 else "not churn"
    probability_pct = round(raw_probability * 100, 2)
    risk_details = classify_risk(probability_pct)

    return jsonify(
        {
            "status": "ok",
            "probability": format_probability(raw_probability),
            "probability_pct": probability_pct,
            "churn_label": churn_label,
            "risk_level": risk_details["risk_level"],
        }
    )


@app.route("/api/history", methods=["GET"])
@login_required
def api_history_list():
    records = (
        PredictionRecord.query.filter_by(org_id=current_user.org_id, user_id=current_user.id)
        .order_by(PredictionRecord.created_at.desc())
        .all()
    )
    items = []
    for record in records:
        items.append(
            {
                "id": record.id,
                "client_reference": record.client_reference,
                "source_type": record.source_type,
                "probability": float(record.probability or 0.0),
                "probability_pct": round(float(record.probability or 0.0) * 100, 2),
                "churn_label": record.decision,
                "created_at": timezoneize(record.created_at).isoformat(),
            }
        )
    return jsonify({"status": "ok", "items": items})


@app.route("/api/history/<int:record_id>", methods=["GET"])
@login_required
def api_history_get(record_id: int):
    record = PredictionRecord.query.get_or_404(record_id)
    validate_or_abort_org_access(record.org_id, record.user_id)
    payload = deserialize_input_data(record.input_data)
    return jsonify(
        {
            "status": "ok",
            "item": {
                "id": record.id,
                "client_reference": record.client_reference,
                "notes": record.notes,
                "source_type": record.source_type,
                "probability": float(record.probability or 0.0),
                "probability_pct": round(float(record.probability or 0.0) * 100, 2),
                "churn_label": record.decision,
                "created_at": timezoneize(record.created_at).isoformat(),
                "input_data": payload,
            },
        }
    )


@app.route("/api/history/<int:record_id>", methods=["PUT"])
@login_required
def api_history_update(record_id: int):
    record = PredictionRecord.query.get_or_404(record_id)
    validate_or_abort_org_access(record.org_id, record.user_id)
    payload = request.get_json(silent=True) or {}
    input_data = payload.get("input_data")
    if not isinstance(input_data, dict):
        return _api_error("Champ 'input_data' manquant.", 400)

    try:
        normalized, enriched_inputs = normalize_record(input_data)
    except ValueError as exc:
        return _api_error(str(exc), 422)

    probabilities, decisions = run_prediction([normalized])
    raw_probability = float(probabilities[0])
    decision_code = int(decisions[0])
    decision = "churn" if decision_code == 1 else "not churn"

    payload_ready = extract_history_payload(enriched_inputs)
    input_hash = compute_input_hash(payload_ready, normalized)
    profile_fields = build_record_profile_fields(payload_ready, raw_probability)
    metadata = build_model_metadata("api-history-update")

    record.client_reference = (payload.get("client_reference") or "").strip() or None
    record.notes = (payload.get("notes") or "").strip() or None
    record.input_data = json.dumps(payload_ready)
    record.probability = format_probability(raw_probability)
    record.decision = decision
    record.input_hash = input_hash
    record.model_metadata = json.dumps(metadata)
    record.payment_method = payload_ready.get("Payment Method Simplified")
    for field, value in profile_fields.items():
        setattr(record, field, value)
    db.session.commit()
    invalidate_analysis_cache()

    return jsonify({"status": "ok", "id": record.id})


@app.route("/api/history/<int:record_id>", methods=["DELETE"])
@login_required
def api_history_delete(record_id: int):
    record = PredictionRecord.query.get_or_404(record_id)
    validate_or_abort_org_access(record.org_id, record.user_id)
    db.session.delete(record)
    db.session.commit()
    invalidate_analysis_cache()
    return jsonify({"status": "ok"})


@app.route("/api/analytics/summary")
@login_required
def api_analytics_summary():
    response = analytics()
    if isinstance(response, tuple):
        return response
    if response.mimetype == "application/json":
        return response
    return redirect(url_for("analytics", format="json"), code=302)



@app.route("/history/<int:record_id>/pdf")
@login_required
def history_pdf(record_id):
    record = PredictionRecord.query.get_or_404(record_id)
    validate_or_abort_org_access(record.org_id, record.user_id)
    organization = Organization.query.get(record.org_id)
    input_data = deserialize_input_data(record.input_data)

    try:
        normalized, enriched = normalize_record(input_data)
    except ValueError:
        normalized = {}
        enriched = input_data
    payload = extract_history_payload({**input_data, **enriched})

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"Churn Report - {record.client_reference or 'Client'}")
    width, height = A4
    margin = 50
    y = height - margin

    probability_percent = record.probability * 100
    predicted_at = timezoneize(record.created_at)
    tracking_id = record.input_hash or compute_input_hash(payload, normalized)

    logo_width = 0
    logo_path = BASE_DIR / "static" / "img" / "logo.png"
    if logo_path.exists():
        try:
            logo = ImageReader(str(logo_path))
            logo_height = 40
            logo_width = logo_height * 2.5
            pdf.drawImage(logo, margin, y - logo_height, width=logo_width, height=logo_height, mask='auto')
        except Exception:
            logo_width = 0

    header_x = margin + (logo_width + 10 if logo_width else 0)
    pdf.setFont("Helvetica-Bold", 16)
    pdf.setFillColor(colors.HexColor("#0f172a"))
    pdf.drawString(header_x, y, "Rapport de prédiction client")
    y -= 24
    pdf.setFont("Helvetica", 10)
    pdf.setFillColor(colors.HexColor("#475569"))
    pdf.drawString(header_x, y, f"Organisation : {organization.name}")
    y -= 14
    pdf.drawString(header_x, y, f"Date de prédiction : {predicted_at.strftime('%d/%m/%Y %H:%M %Z')}")
    y -= 14
    pdf.drawString(header_x, y, f"Référence client : {record.client_reference or 'N/A'}")
    y -= 14
    pdf.drawString(header_x, y, f"ID de traçabilité : {tracking_id}")
    y -= 20

    try:
        qr_code = qr.QrCodeWidget(tracking_id)
        bounds = qr_code.getBounds()
        size = 70
        drawing = Drawing(size, size)
        drawing.add(qr_code)
        renderPDF.draw(drawing, pdf, width - margin - size, height - margin - size)
    except Exception:
        pass

    pdf.setStrokeColor(colors.HexColor("#cbd5f5"))
    pdf.line(margin, y, width - margin, y)
    y -= 28

    pdf.setFont("Helvetica-Bold", 12)
    pdf.setFillColor(colors.HexColor("#0f172a"))
    pdf.drawString(margin, y, "Résultat de la prédiction")
    y -= 18
    pdf.setFont("Helvetica", 10)
    pdf.setFillColor(colors.HexColor("#0f172a"))
    pdf.drawString(margin, y, f"Probabilité de churn : {probability_percent:.2f} %")
    bar_width = width - 2 * margin
    bar_height = 12
    y -= 16
    pdf.setStrokeColor(colors.HexColor("#dce3f5"))
    pdf.rect(margin, y, bar_width, bar_height, stroke=1, fill=0)
    pdf.setFillColor(colors.HexColor("#2563eb"))
    pdf.rect(margin, y, bar_width * min(max(record.probability, 0.0), 1.0), bar_height, stroke=0, fill=1)
    y -= (bar_height + 14)
    badge_width = 140
    badge_height = 18
    badge_color = colors.HexColor("#dc2626") if record.decision == "churn" else colors.HexColor("#16a34a")
    pdf.setFillColor(badge_color)
    pdf.roundRect(margin, y - badge_height, badge_width, badge_height, 4, stroke=0, fill=1)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawCentredString(margin + badge_width / 2, y - badge_height + 5, record.decision)
    y -= (badge_height + 12)

    pdf.setStrokeColor(colors.HexColor("#cbd5f5"))
    pdf.line(margin, y, width - margin, y)
    y -= 24
    pdf.setFont("Helvetica-Bold", 12)
    pdf.setFillColor(colors.HexColor("#0f172a"))
    pdf.drawString(margin, y, "Caractéristiques analysées")
    y -= 16
    pdf.setFont("Helvetica", 9)
    pdf.setFillColor(colors.HexColor("#1f2937"))

    feature_pairs = []
    for feature in BASE_FEATURES + list(DERIVED_FEATURES):
        feature_pairs.append((feature, format_feature_value(feature, payload.get(feature))))

    columns = 2
    col_width = (width - 2 * margin) / columns
    row_height = 18
    for idx, (label, value) in enumerate(feature_pairs):
        row_idx = idx // columns
        col_idx = idx % columns
        current_y = y - row_idx * row_height
        x = margin + col_idx * col_width
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(x, current_y, label)
        pdf.setFont("Helvetica", 9)
        pdf.drawString(x, current_y - 10, str(value))
    y -= (row_height * ((len(feature_pairs) + (columns - 1)) // columns) + 20)

    if record.notes:
        pdf.setStrokeColor(colors.HexColor("#cbd5f5"))
        pdf.line(margin, y, width - margin, y)
        y -= 20
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(margin, y, "Notes opérationnelles")
        y -= 16
        pdf.setFont("Helvetica", 9)
        lines = record.notes.splitlines()
        max_lines = 6
        for idx, line in enumerate(lines[:max_lines]):
            pdf.drawString(margin, y, line)
            y -= 12
        if len(lines) > max_lines:
            pdf.drawString(margin, y, "...")
            y -= 12

    metadata = {}
    if record.model_metadata:
        try:
            metadata = json.loads(record.model_metadata)
        except json.JSONDecodeError:
            metadata = {}

    pdf.setStrokeColor(colors.HexColor("#cbd5f5"))
    pdf.line(margin, y, width - margin, y)
    y -= 20
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(margin, y, "Métadonnées du modèle")
    y -= 16
    pdf.setFont("Helvetica", 9)
    pdf.drawString(margin, y, f"Modèle : {metadata.get('model_path', 'N/A')}")
    y -= 12
    pdf.drawString(margin, y, f"Checksum schéma : {metadata.get('schema_checksum', 'N/A')}")
    y -= 12
    pdf.drawString(margin, y, f"Tracking ID : {tracking_id}")
    y -= 12
    pdf.drawString(margin, y, "Utilisation : rapport interne - décisions sous responsabilité métier")
    y -= 18

    pdf.setStrokeColor(colors.HexColor("#cbd5f5"))
    pdf.line(margin, margin + 30, width - margin, margin + 30)
    pdf.setFont("Helvetica", 8)
    pdf.setFillColor(colors.HexColor("#475569"))
    pdf.drawString(margin, margin + 16, "© TelcoAI Predict – Support : support@telcoai.com")
    pdf.drawRightString(width - margin, margin + 16, "Page 1 / 1")

    pdf.save()
    buffer.seek(0)

    filename = f"prediction_{record.id}.pdf"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype="application/pdf")


def load_history_dataframe(org_id: int | None = None, user_id: int | None = None) -> pd.DataFrame:
    query = PredictionRecord.query.order_by(PredictionRecord.created_at.asc())
    if org_id is not None:
        query = query.filter_by(org_id=org_id)
    if user_id is not None:
        query = query.filter_by(user_id=user_id)
    records = query.all()
    rows = []
    for record in records:
        input_data = deserialize_input_data(record.input_data)
        try:
            normalized, enriched = normalize_record(input_data)
        except ValueError:
            normalized = {}
            enriched = input_data
        payload = extract_history_payload({**input_data, **enriched})
        row = {
            "record_id": record.id,
            "org_id": record.org_id,
            "user_id": record.user_id,
            "source_type": record.source_type,
            "batch_id": record.batch_id,
            "created_at": record.created_at,
            "predicted_at": timezoneize(record.created_at),
            "probability": float(record.probability),
            "decision": record.decision,
            "is_churn": 1 if record.decision == "churn" else 0,
            "client_reference": record.client_reference,
            "Avg Charges per Month": coerce_float(payload.get("Avg Charges per Month")),
            "Has Long Tenure": coerce_bool(payload.get("Has Long Tenure")),
            "IsMonthToMonth": coerce_bool(payload.get("IsMonthToMonth")),
            "Payment Method Simplified": payload.get("Payment Method Simplified") or "Other",
            "Tenure Months": coerce_int(payload.get("Tenure Months")),
            "Monthly Charges": coerce_float(payload.get("Monthly Charges")),
            "Total Charges": coerce_float(payload.get("Total Charges")),
            "input_hash": record.input_hash,
        }
        if not row["Payment Method Simplified"] and record.payment_method:
            row["Payment Method Simplified"] = record.payment_method
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["created_at"] = pd.to_datetime(df["created_at"])
    if df["created_at"].dt.tz is None:
        df["created_at"] = df["created_at"].dt.tz_localize("UTC")
    df["predicted_at"] = pd.to_datetime(df["predicted_at"]).dt.tz_convert(AFRICA_CASABLANCA)
    return df


def parse_analysis_filters(args, apply_default_window: bool = True):
    def parse_date(value):
        if not value:
            return None, ""
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
            return parsed, value
        except ValueError:
            return None, ""

    now_local = datetime.now(AFRICA_CASABLANCA)

    start_date_raw, start_str = parse_date(args.get("start_date"))
    if start_date_raw:
        start_date = start_date_raw.replace(tzinfo=AFRICA_CASABLANCA)
    elif apply_default_window:
        start_date = now_local - timedelta(weeks=12)
        start_str = start_date.strftime("%Y-%m-%d")
    else:
        start_date = None
        start_str = ""

    end_date_raw, end_str = parse_date(args.get("end_date"))
    if end_date_raw:
        end_date = end_date_raw.replace(
            tzinfo=AFRICA_CASABLANCA, hour=23, minute=59, second=59, microsecond=999999
        )
    elif apply_default_window:
        end_date = now_local.replace(hour=23, minute=59, second=59, microsecond=999999)
        end_str = end_date.strftime("%Y-%m-%d")
    else:
        end_date = None
        end_str = ""

    probability_str = args.get("probability_min", "").strip()
    probability_min = 0.0
    if probability_str:
        try:
            value = float(probability_str.replace(",", "."))
            probability_min = value / 100 if value > 1 else value
        except ValueError:
            probability_min = 0.0

    long_tenure_choice = args.get("has_long_tenure", "all")
    if long_tenure_choice in {"true", "false"}:
        has_long_tenure = long_tenure_choice == "true"
    else:
        has_long_tenure = None

    month_choice = args.get("is_month_to_month", "all")
    if month_choice in {"true", "false"}:
        is_month_to_month = month_choice == "true"
    else:
        is_month_to_month = None

    payment_methods = args.getlist("payment_methods") or []

    filters = {
        "start_date": start_date,
        "end_date": end_date,
        "probability_min": probability_min,
        "has_long_tenure": has_long_tenure,
        "is_month_to_month": is_month_to_month,
        "payment_methods": payment_methods,
    }

    form_values = {
        "start_date": start_str,
        "end_date": end_str,
        "probability_min": probability_str,
        "has_long_tenure": long_tenure_choice,
        "is_month_to_month": month_choice,
        "payment_methods": payment_methods,
    }
    return filters, form_values


def apply_analysis_filters(df: pd.DataFrame, filters: dict) -> pd.DataFrame:
    if df.empty:
        return df
    filtered = df.copy()
    if filters.get("start_date"):
        filtered = filtered[filtered["predicted_at"] >= filters["start_date"]]
    if filters.get("end_date"):
        filtered = filtered[filtered["predicted_at"] <= filters["end_date"]]
    if filters.get("probability_min"):
        filtered = filtered[filtered["probability"] >= filters["probability_min"]]
    if filters.get("has_long_tenure") is not None:
        filtered = filtered[filtered["Has Long Tenure"] == filters["has_long_tenure"]]
    if filters.get("is_month_to_month") is not None:
        filtered = filtered[filtered["IsMonthToMonth"] == filters["is_month_to_month"]]
    if filters.get("payment_methods"):
        filtered = filtered[
            filtered["Payment Method Simplified"].isin(filters["payment_methods"])
        ]
    return filtered


def build_segment_summary(df: pd.DataFrame, column: str, label_func) -> list[dict]:
    if df.empty:
        return []
    summary = (
        df.groupby(column)
        .agg(
            total=("probability", "count"),
            churn_rate=("is_churn", "mean"),
            avg_probability=("probability", "mean"),
            avg_charge=("Avg Charges per Month", "mean"),
        )
        .reset_index()
    )
    results = []
    for _, row in summary.iterrows():
        if row["total"] == 0:
            continue
        results.append(
            {
                "label": label_func(row[column]),
                "total": int(row["total"]),
                "churn_rate": round(float(row["churn_rate"]) * 100, 2)
                if pd.notna(row["churn_rate"])
                else 0.0,
                "avg_probability": round(float(row["avg_probability"]), 4)
                if pd.notna(row["avg_probability"])
                else 0.0,
                "avg_charge": round(float(row["avg_charge"]), 2)
                if pd.notna(row["avg_charge"])
                else None,
            }
        )
    return results


def build_top_segments(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    working = df.copy()
    working["pm_label"] = working["Payment Method Simplified"].fillna("Autre")
    working["tenure_label"] = working["Has Long Tenure"].map(
        lambda x: "Ancienneté longue" if x else "Ancienneté courte"
    )
    working["contract_label"] = working["IsMonthToMonth"].map(
        lambda x: "Mensuel" if x else "Engagement"
    )
    working["segment_label"] = (
        working["pm_label"] + " · " + working["tenure_label"] + " · " + working["contract_label"]
    )
    summary = (
        working.groupby("segment_label")
        .agg(
            total=("probability", "count"),
            churn_rate=("is_churn", "mean"),
            avg_probability=("probability", "mean"),
            avg_charge=("Avg Charges per Month", "mean"),
        )
        .reset_index()
    )
    summary = summary[summary["total"] > 0]
    summary = summary.sort_values("avg_probability", ascending=False).head(5)
    top_segments = []
    for _, row in summary.iterrows():
        top_segments.append(
            {
                "label": row["segment_label"],
                "total": int(row["total"]),
                "churn_rate": round(float(row["churn_rate"]) * 100, 2)
                if pd.notna(row["churn_rate"])
                else 0.0,
                "avg_probability": round(float(row["avg_probability"]), 4)
                if pd.notna(row["avg_probability"])
                else 0.0,
                "avg_charge": round(float(row["avg_charge"]), 2)
                if pd.notna(row["avg_charge"])
                else None,
            }
        )
    return top_segments


def build_timeline_summary(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"labels": [], "churn_rates": [], "totals": [], "frequency": "hebdomadaire"}
    timeline_df = df.copy()
    timeline_df["predicted_at"] = pd.to_datetime(timeline_df["predicted_at"])
    timeline_df["predicted_at"] = timeline_df["predicted_at"].dt.tz_convert(AFRICA_CASABLANCA)
    timeline_df = timeline_df.sort_values("predicted_at")
    grouped = (
        timeline_df.set_index("predicted_at")
        .resample("W-MON", label="left", closed="left")
        .agg(total=("probability", "count"), churn_rate=("is_churn", "mean"))
    )
    grouped = grouped[grouped["total"] > 0]
    labels = []
    churn_rates = []
    totals = []
    for index, row in grouped.iterrows():
        week_start = index
        week_end = index + timedelta(days=6)
        labels.append(
            f"Semaine du {week_start.strftime('%d/%m')} au {week_end.strftime('%d/%m')}"
        )
        churn_rates.append(
            round(float(row["churn_rate"]) * 100, 2) if pd.notna(row["churn_rate"]) else 0.0
        )
        totals.append(int(row["total"]))
    return {
        "labels": labels,
        "churn_rates": churn_rates,
        "totals": totals,
        "frequency": "hebdomadaire",
    }


def build_scatter_points(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    scatter_source = df.dropna(subset=["Avg Charges per Month"])
    points = []
    for _, row in scatter_source.iterrows():
        points.append(
            {
                "x": round(float(row["Avg Charges per Month"]), 2),
                "y": round(float(row["probability"]), 4),
                "label": "Mensuel" if row["IsMonthToMonth"] else "Engagement",
            }
        )
    return points


def build_export_rows(segment_data: dict, timeline_data: dict, top_segments: list[dict], timeframe: str):
    rows = []
    for segment_type, dataset in segment_data.items():
        for entry in dataset:
            rows.append(
                {
                    "Segment Type": segment_type,
                    "Segment": entry["label"],
                    "Total Predictions": entry["total"],
                    "Predicted Churn %": entry["churn_rate"],
                    "Average Probability": entry["avg_probability"],
                    "Average Avg Charges": entry["avg_charge"],
                    "Timeframe": timeframe,
                }
            )
    for label, churn_rate, total in zip(
        timeline_data.get("labels", []),
        timeline_data.get("churn_rates", []),
        timeline_data.get("totals", []),
    ):
        rows.append(
            {
                "Segment Type": f"Timeline ({timeline_data.get('frequency')})",
                "Segment": label,
                "Total Predictions": total,
                "Predicted Churn %": churn_rate,
                "Average Probability": None,
                "Average Avg Charges": None,
                "Timeframe": timeframe,
            }
        )
    for entry in top_segments:
        rows.append(
            {
                "Segment Type": "Top Risk Segment",
                "Segment": entry["label"],
                "Total Predictions": entry["total"],
                "Predicted Churn %": entry["churn_rate"],
                "Average Probability": entry["avg_probability"],
                "Average Avg Charges": entry["avg_charge"],
                "Timeframe": timeframe,
            }
        )
    return rows


def prepare_analysis_payload(user: User, filters: dict) -> dict:
    cache_key = build_analysis_cache_key(user, filters)
    cached = get_cached_analysis(cache_key)
    if cached:
        return cached

    scope_org_id = get_user_scope(user)
    user_scope_id = user.id
    base_df = load_history_dataframe(scope_org_id, user_scope_id)
    available_payment_methods = (
        sorted(base_df["Payment Method Simplified"].dropna().unique())
        if not base_df.empty and "Payment Method Simplified" in base_df.columns
        else []
    )

    filtered_df = apply_analysis_filters(base_df, filters)
    total_predictions = int(filtered_df.shape[0]) if not filtered_df.empty else 0
    analysis_ready = total_predictions > 0
    reference_df = filtered_df if analysis_ready else base_df
    timeframe_text = "Aucune donnée disponible"
    timeframe_bounds = {"start": None, "end": None}

    if not reference_df.empty:
        timeframe_start = reference_df["predicted_at"].min()
        timeframe_end = reference_df["predicted_at"].max()
        if timeframe_start is not None and timeframe_end is not None:
            timeframe_start = timeframe_start.tz_convert(AFRICA_CASABLANCA)
            timeframe_end = timeframe_end.tz_convert(AFRICA_CASABLANCA)
            timeframe_text = (
                f"du {timeframe_start.strftime('%d/%m/%Y')} au {timeframe_end.strftime('%d/%m/%Y')}"
            )
            timeframe_bounds = {
                "start": timeframe_start.isoformat(),
                "end": timeframe_end.isoformat(),
            }

    key_metrics = {}
    if analysis_ready:
        churn_rate = filtered_df["is_churn"].mean() * 100 if total_predictions else 0
        avg_charge = filtered_df["Avg Charges per Month"].dropna().mean()
        key_metrics = {
            "Clients scorés": total_predictions,
            "Churn prédit (%)": round(churn_rate, 2) if total_predictions else 0,
            "Charge moyenne (€/mois)": round(avg_charge, 2) if pd.notna(avg_charge) else None,
        }

    segment_payment = build_segment_summary(
        filtered_df,
        "Payment Method Simplified",
        lambda value: value or "Autre",
    )
    segment_tenure = build_segment_summary(
        filtered_df,
        "Has Long Tenure",
        lambda value: "Oui" if value else "Non",
    )
    segment_contract = build_segment_summary(
        filtered_df,
        "IsMonthToMonth",
        lambda value: "Mensuel" if value else "Engagement",
    )

    segment_data = {
        "Mode de paiement": segment_payment,
        "Ancienneté longue": segment_tenure,
        "Type de contrat": segment_contract,
    }

    chart_states = {
        "payment": {"empty": not segment_payment, "message": EMPTY_STATE_MESSAGE},
        "tenure": {"empty": not segment_tenure, "message": EMPTY_STATE_MESSAGE},
        "contract": {"empty": not segment_contract, "message": EMPTY_STATE_MESSAGE},
    }

    top_segments = build_top_segments(filtered_df)
    timeline_data = build_timeline_summary(filtered_df)
    scatter_points = build_scatter_points(filtered_df)
    export_rows = build_export_rows(segment_data, timeline_data, top_segments, timeframe_text)

    payload = {
        "analysis_ready": analysis_ready,
        "has_history": not base_df.empty,
        "key_metrics": key_metrics,
        "segment_data": segment_data,
        "top_segments": top_segments,
        "timeline_data": timeline_data,
        "scatter_points": scatter_points,
        "total_predictions": total_predictions,
        "timeframe_text": timeframe_text,
        "timeframe_bounds": timeframe_bounds,
        "available_payment_methods": available_payment_methods,
        "chart_states": chart_states,
        "N": total_predictions,
        "empty": total_predictions == 0,
        "export_rows": export_rows,
        "cache_key": cache_key,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

    set_cached_analysis(cache_key, payload)
    return payload


# --- Analysis routes ---


def parse_history_analysis_filters(args, include_user: bool = False) -> dict:
    start_raw = (args.get("start_date") or "").strip()
    end_raw = (args.get("end_date") or "").strip()
    risk_raw = (args.get("risk") or "all").strip().lower()
    source_raw = (args.get("source") or "all").strip().lower()
    user_raw = (args.get("user_id") or "").strip() if include_user else ""

    def parse_bound(value: str) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None

    start_dt = parse_bound(start_raw)
    end_dt = parse_bound(end_raw)
    start_bound = datetime.combine(start_dt, datetime.min.time()) if start_dt else None
    end_bound = datetime.combine(end_dt, datetime.min.time()) + timedelta(days=1) if end_dt else None

    if risk_raw not in {"high", "medium", "low"}:
        risk_raw = "all"
    if source_raw not in {"individual", "batch"}:
        source_raw = "all"

    user_value = None
    if include_user and user_raw.isdigit():
        user_value = int(user_raw)

    return {
        "start_dt": start_bound,
        "end_dt": end_bound,
        "risk": risk_raw,
        "source": source_raw,
        "user_id": user_value if include_user else None,
        "form": {
            "start_date": start_raw,
            "end_date": end_raw,
            "risk": risk_raw,
            "source": source_raw,
            "user_id": user_raw if include_user else "",
        },
    }


def build_history_analysis_dataframe(records: list[PredictionRecord]) -> pd.DataFrame:
    rows: list[dict] = []
    for record in records:
        try:
            probability_value = float(record.probability or 0.0)
        except (TypeError, ValueError):
            probability_value = 0.0
        if probability_value > 1.0:
            probability_value = probability_value / 100.0
        created_at = record.created_at or datetime.utcnow()
        created_local = timezoneize(created_at)
        resolved_risk = record.risk_level or classify_risk(probability_value * 100)["risk_level"]
        rows.append(
            {
                "id": record.id,
                "probability": probability_value,
                "risk_level": resolved_risk,
                "contract": record.contract,
                "internet_service": record.internet_service,
                "tenure_months": record.tenure_months,
                "monthly_charges": record.monthly_charges,
                "created_at": created_local,
                "source": record.source_type or "individual",
                "customer_reference": record.client_reference,
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df["created_at"] = pd.to_datetime(df["created_at"])
        df["probability_pct"] = df["probability"] * 100
        df["risk_level"] = df["risk_level"].str.lower()
        df["monthly_charges"] = pd.to_numeric(df["monthly_charges"], errors="coerce")
        df["tenure_months"] = pd.to_numeric(df["tenure_months"], errors="coerce")
    return df


def compute_history_kpis(df: pd.DataFrame) -> dict:
    if df.empty:
        return {
            "total_predictions": 0,
            "high_percent": 0.0,
            "medium_percent": 0.0,
            "low_percent": 0.0,
            "avg_probability": 0.0,
            "unique_customers": 0,
        }

    total = int(df.shape[0])
    distribution = df["risk_level"].value_counts(dropna=False)
    high_pct = round((distribution.get("high", 0) / total) * 100, 2)
    medium_pct = round((distribution.get("medium", 0) / total) * 100, 2)
    low_pct = round((distribution.get("low", 0) / total) * 100, 2)
    avg_probability = round(float(df["probability_pct"].mean()), 2)
    unique_customers = (
        int(df["customer_reference"].dropna().replace("", pd.NA).nunique())
        if "customer_reference" in df.columns
        else 0
    )
    return {
        "total_predictions": total,
        "high_percent": high_pct,
        "medium_percent": medium_pct,
        "low_percent": low_pct,
        "avg_probability": avg_probability,
        "unique_customers": unique_customers,
    }


def build_history_charts(df: pd.DataFrame) -> dict:
    charts = {
        "risk_distribution": {"labels": ["Risque élevé", "Risque moyen", "Risque faible"], "values": [0, 0, 0]},
        "time_series": {"labels": [], "counts": [], "avg_probabilities": []},
        "risk_by_contract": {"labels": [], "values": []},
        "risk_by_tenure_group": {"labels": [], "high_pct": [], "counts": []},
        "charges_by_risk": {
            "labels": ["Risque élevé", "Risque moyen", "Risque faible"],
            "values": [0, 0, 0],
            "counts": [0, 0, 0],
        },
    }
    if df.empty:
        return charts

    distribution = df["risk_level"].value_counts()
    charts["risk_distribution"]["values"] = [
        int(distribution.get("high", 0)),
        int(distribution.get("medium", 0)),
        int(distribution.get("low", 0)),
    ]

    timeline_df = df.copy()
    timeline_df["created_date"] = timeline_df["created_at"].dt.floor("D")
    grouped = (
        timeline_df.groupby("created_date")
        .agg(total=("id", "count"), avg_probability=("probability_pct", "mean"))
        .reset_index()
        .sort_values("created_date")
    )
    charts["time_series"]["labels"] = [
        row.strftime("%d/%m/%Y") for row in grouped["created_date"].dt.tz_localize(None)
    ]
    charts["time_series"]["counts"] = [int(value) for value in grouped["total"]]
    charts["time_series"]["avg_probabilities"] = [round(float(value), 2) for value in grouped["avg_probability"]]

    contract_df = df.dropna(subset=["contract"])
    if not contract_df.empty:
        contract_order = ["Month-to-month", "One year", "Two year"]
        seen = set()
        for label in contract_order:
            subset = contract_df[contract_df["contract"] == label]
            if subset.empty:
                continue
            seen.add(label)
            share = round(((subset["risk_level"] == "high").mean() or 0) * 100, 2)
            charts["risk_by_contract"]["labels"].append(label)
            charts["risk_by_contract"]["values"].append(share)
        remaining = sorted(set(contract_df["contract"]) - seen)
        for label in remaining:
            subset = contract_df[contract_df["contract"] == label]
            share = round(((subset["risk_level"] == "high").mean() or 0) * 100, 2)
            charts["risk_by_contract"]["labels"].append(label)
            charts["risk_by_contract"]["values"].append(share)

    tenure_df = df.dropna(subset=["tenure_months"])
    if not tenure_df.empty:
        bins = [-1, 12, 24, 48, float("inf")]
        labels = ["0–12 mois", "13–24 mois", "25–48 mois", "48+ mois"]
        tenure_df = tenure_df.copy()
        tenure_df["tenure_group"] = pd.cut(tenure_df["tenure_months"], bins=bins, labels=labels)
        group_stats = (
            tenure_df.groupby("tenure_group")
            .agg(counts=("id", "count"), high_share=("risk_level", lambda s: (s == "high").mean() * 100))
            .reindex(labels)
        )
        charts["risk_by_tenure_group"]["labels"] = labels
        charts["risk_by_tenure_group"]["counts"] = [
            int(value) if pd.notna(value) else 0 for value in group_stats["counts"].tolist()
        ]
        charts["risk_by_tenure_group"]["high_pct"] = [
            round(float(value), 2) if pd.notna(value) else 0 for value in group_stats["high_share"].tolist()
        ]

    charges_df = df.dropna(subset=["monthly_charges"])
    if not charges_df.empty:
        risk_levels = ["high", "medium", "low"]
        for index, level in enumerate(risk_levels):
            subset = charges_df[charges_df["risk_level"] == level]["monthly_charges"]
            charts["charges_by_risk"]["counts"][index] = int(subset.shape[0])
            if subset.empty:
                charts["charges_by_risk"]["values"][index] = 0
            else:
                charts["charges_by_risk"]["values"][index] = round(float(subset.mean()), 2)

    return charts


def generate_history_insights(df: pd.DataFrame, kpis: dict, charts: dict) -> list[str]:
    if df.empty or not kpis.get("total_predictions"):
        return ["Aucune donnée disponible pour l'instant. Enregistrez des prédictions pour alimenter cette analyse."]

    insights: list[str] = []
    insights.append(
        f"Sur la période sélectionnée, {kpis['high_percent']} % des {kpis['total_predictions']} prédictions sont classées à risque élevé."
    )
    insights.append(
        f"La probabilité moyenne de churn observée est de {kpis['avg_probability']} %."
    )
    if kpis.get("unique_customers"):
        insights.append(f"Cette analyse couvre {kpis['unique_customers']} clients uniques.")

    contract_chart = charts.get("risk_by_contract", {})
    contract_values = contract_chart.get("values") or []
    contract_labels = contract_chart.get("labels") or []
    if contract_labels and contract_values:
        idx = int(max(range(len(contract_values)), key=lambda i: contract_values[i]))
        insights.append(
            f"Les contrats « {contract_labels[idx]} » affichent {contract_values[idx]} % de clients à risque élevé."
        )

    tenure_chart = charts.get("risk_by_tenure_group", {})
    tenure_values = tenure_chart.get("high_pct") or []
    tenure_labels = tenure_chart.get("labels") or []
    if tenure_labels and any(value > 0 for value in tenure_values):
        idx = int(max(range(len(tenure_values)), key=lambda i: tenure_values[i]))
        insights.append(
            f"Le groupe d'ancienneté {tenure_labels[idx]} concentre {round(tenure_values[idx], 2)} % de clients à haut risque."
        )

    charges_values = charts.get("charges_by_risk", {}).get("values") or []
    if len(charges_values) == 3 and charges_values[0] and charges_values[2]:
        insights.append(
            f"Les clients à haut risque dépensent en moyenne {charges_values[0]} € par mois, contre {charges_values[2]} € pour les clients à faible risque."
        )

    time_series = charts.get("time_series", {})
    counts = time_series.get("counts") or []
    labels = time_series.get("labels") or []
    if labels and counts:
        idx = int(max(range(len(counts)), key=lambda i: counts[i]))
        insights.append(
            f"La journée du {labels[idx]} enregistre le pic d'activité avec {counts[idx]} prédictions effectuées."
        )

    return insights[:6]


@app.route("/analytics")
@app.route("/analysis")
@login_required
def analytics():
    filters_payload = parse_history_analysis_filters(request.args, include_user=False)
    query = PredictionRecord.query.filter_by(org_id=current_user.org_id, user_id=current_user.id)
    if filters_payload["start_dt"]:
        query = query.filter(PredictionRecord.created_at >= filters_payload["start_dt"])
    if filters_payload["end_dt"]:
        query = query.filter(PredictionRecord.created_at < filters_payload["end_dt"])
    if filters_payload["source"] == "individual":
        query = query.filter(PredictionRecord.source_type != "batch")
    elif filters_payload["source"] == "batch":
        query = query.filter(PredictionRecord.source_type == "batch")

    records = query.order_by(PredictionRecord.created_at.asc()).all()
    df = build_history_analysis_dataframe(records)
    if not df.empty and filters_payload["risk"] != "all":
        df = df[df["risk_level"] == filters_payload["risk"]]

    has_data = not df.empty
    kpis = compute_history_kpis(df)
    charts = build_history_charts(df)
    insights = generate_history_insights(df, kpis, charts)

    if wants_json_response():
        return jsonify(
            {
                "status": "ok",
                "kpis": kpis,
                "charts": charts,
                "insights": insights,
                "filters": filters_payload["form"],
                "has_data": has_data,
            }
        )

    return render_template(
        "analysis.html",
        kpis=kpis,
        charts=charts,
        insights=insights,
        filters=filters_payload["form"],
        has_data=has_data,
    )


# --- Error handlers ---


@app.errorhandler(403)
def forbidden(_error):
    return render_template("errors/403.html"), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template("errors/404.html"), 404


@app.errorhandler(413)
def payload_too_large(_error):
    flash("Le fichier fourni est trop volumineux.", "error")
    return redirect(request.referrer or url_for("predict_individual"))


@app.context_processor
def inject_globals():
    return {
        "current_year": datetime.utcnow().year,
        "format_feature_value": format_feature_value,
        "feature_metadata_map": feature_metadata_map,
        "get_long_tenure_threshold": get_long_tenure_threshold,
        "feature_form_key": feature_form_key,
        "normalize_column_key": normalize_column_key,
    }


if __name__ == "__main__":
    app.run(debug=True)
