# Geonorge Eiendomsteig → GPX/KML (One‑click Render)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/sondrestormo/Geonorge-gpx)

> Viktig: Denne versjonen bruker `https://nedlasting.geonorge.no/api/order?api-version=2.0`
> (altså API‑versjon i query, ikke i header) for å unngå UnsupportedApiVersion‑feilen.

## Lokal kjøring
```
pip install -r requirements.txt
python app.py
# åpne http://localhost:5000
```

## One‑click deploy
1. Lag et offentlig GitHub‑repo og last opp alle filene her.
2. Bytt ut `<REPLACE_WITH_YOUR_GITHUB_REPO_URL>` i knappen over med din repo‑URL.
3. Klikk knappen i README – Render finner `render.yaml` og setter alt opp automatisk.
