# Teleco Churn

Application web Flask (frontend + backend + base de données) pour prédire le churn télécom, sauvegarder l’historique, traiter des lots (CSV/Excel) et analyser les résultats.

## Parcours & pages (ordre logique)

1) **Signup / Registration** (`/` → `/register`)
- L’ouverture de l’app affiche l’inscription (infos entreprise + infos utilisateur).
- Après inscription : redirection vers **Home**.

2) **Home** (`/home`)
- Layout principal avec navigation vers : **Predict (individual)**, **History**, **Predict (dataset/batch)**, **Analytics**.

3) **Predict (Individual)** (`/predict/individual`)
- Formulaire client → probabilité (%) + churn label (`churn` / `not churn`)
- Bouton : enregistrer dans l’historique.

4) **History (Saved predictions)** (`/history`)
- Liste des clients enregistrés (individuel + batch)
- Actions : modifier les informations client, supprimer, télécharger le PDF du client
- Export : télécharger le dataset complet (`/history/export`)

5) **Predict (Dataset / Batch)** (`/predict/batch`)
- Import CSV/Excel → affichage tableau éditable → prédiction batch
- Après prédiction : enregistrer tous les résultats dans l’historique OU exporter les prédictions en CSV/Excel

6) **Analytics** (`/analytics`)
- Graphiques et insights basés sur les clients enregistrés dans l’app.

## Architecture & structure

- `app.py` : backend Flask (routes web + REST API)
- `services.py` : services ML (normalisation, prédiction, enrichissement)
- `templates/` : pages frontend (Jinja)
- `static/` : CSS/JS
- `models/` : artefacts ML (`best_model.pkl`, `schema.json`, `input_features.json`)
- `migrations/` + `alembic.ini` : migrations DB (Alembic via Flask-Migrate)
- `instance/` : base SQLite + cache batch

## Base de données (schéma)

Tables principales :
- `organization` : entreprise (signup)
- `user` : utilisateur (signup/login)
- `prediction_record` : client enregistré + résultats (probabilité, churn label, timestamp, source `individual`/`batch`)

## REST API (principaux endpoints)

Auth
- `POST /api/auth/signup`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/auth/me`
- `POST /api/orgs` (signup step 1)
- `POST /api/users` (signup step 2)

Predict
- `POST /api/predict/individual`

History CRUD
- `GET /api/history`
- `GET /api/history/<id>`
- `PUT /api/history/<id>`
- `DELETE /api/history/<id>`
- `GET /history/<id>/pdf`
- `GET /history/export`

Batch (dataset)
- `POST /predict/batch/upload` (CSV/Excel)
- `POST /predict/batch/predict`
- `POST /predict/batch/history`
- `POST /predict/batch/export` (CSV/Excel)

Analytics
- `GET /analytics?format=json`
- `GET /api/analytics/summary`

### Exemples (JSON)

`POST /api/predict/individual`
```json
{
  "client": {
    "Gender": "Male",
    "Senior Citizen": "No",
    "Partner": "Yes",
    "Dependents": "No",
    "Tenure Months": 12,
    "Phone Service": "Yes",
    "Multiple Lines": "No",
    "Internet Service": "Fiber optic",
    "Online Security": "No",
    "Online Backup": "Yes",
    "Device Protection": "No",
    "Tech Support": "No",
    "Streaming TV": "Yes",
    "Streaming Movies": "Yes",
    "Contract": "Month-to-month",
    "Paperless Billing": "Yes",
    "Payment Method": "Electronic check",
    "Monthly Charges": 89.5,
    "Total Charges": 1020.0
  }
}
```

Réponse
```json
{
  "status": "ok",
  "probability": 0.7421,
  "probability_pct": 74.21,
  "churn_label": "churn",
  "risk_level": "high"
}
```

## Lancer en local

Prérequis : Python 3, `pip`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

export FLASK_ENV=development
export SECRET_KEY="dev-secret"

flask --app app db upgrade
flask --app app run
```

Ouvrir : `http://127.0.0.1:5000/` (inscription par défaut).
