from flask import Flask, request, render_template, send_file
import requests, time, tempfile, os, io, zipfile, json
import folium
from shapely.geometry import shape, Polygon, MultiPolygon
import gpxpy, gpxpy.gpx
from fastkml import kml
from pyproj import Transformer

app = Flask(__name__)

# Geonorge Nedlasting API (Matrikkelen – Eiendomskart Teig)
DATASET_UUID = "74340c24-1c8a-4454-b813-bfe498e80f16"
BASE = "https://nedlasting.geonorge.no"
ORDER_URL = f"{BASE}/api/order?api-version=2.0"
STATUS_URL = f"{BASE}/api/order/{{ref}}?api-version=2.0"

# Geonorge Adresse-API
ADRESSE_API = "https://ws.geonorge.no/adresser/v1/sok"

def geocode_adresse(adresse: str):
    r = requests.get(ADRESSE_API, params={"sok": adresse, "treffPerSide": 1}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("adresser"):
        return None
    a = data["adresser"][0]
    if "representasjonspunkt" in a and a["representasjonspunkt"]:
        return float(a["representasjonspunkt"]["lon"]), float(a["representasjonspunkt"]["lat"])
    elif "punkt" in a and a["punkt"]:
        epsg = int(a["punkt"]["epsg"])
        E, N = [float(x) for x in a["punkt"]["koordinater"].split(",")]
        trans = Transformer.from_crs(epsg, 4326, always_xy=True)
        lon, lat = trans.transform(E, N)
        return lon, lat
    return None

def build_square_utm25833(lon, lat, half_m):
    to_utm = Transformer.from_crs(4326, 25833, always_xy=True)
    x, y = to_utm.transform(lon, lat)
    return Polygon([(x-half_m, y-half_m), (x+half_m, y-half_m), (x+half_m, y+half_m), (x-half_m, y+half_m), (x-half_m, y-half_m)])

def tile_polygon(poly_utm: Polygon, tile_m: int):
    minx, miny, maxx, maxy = poly_utm.bounds
    x = minx; tiles = []
    while x < maxx:
        y = miny; nx = min(x + tile_m, maxx)
        while y < maxy:
            ny = min(y + tile_m, maxy)
            tile = Polygon([(x,y),(nx,y),(nx,ny),(x,ny),(x,y)])
            inter = poly_utm.intersection(tile)
            if not inter.is_empty:
                geoms = [inter] if isinstance(inter, Polygon) else list(getattr(inter, "geoms", []))
                for g in geoms:
                    if isinstance(g, Polygon) and g.area > 1.0:
                        tiles.append(g)
            y = ny
        x = nx
    return tiles

def coords_str_from_utm_polygon(poly: Polygon):
    return " ".join([f"{round(px,3)} {round(py,3)}" for (px, py) in list(poly.exterior.coords)])

def order_polygon(coords_str, coord_sys, email, format_name="GeoJSON", projection="4326"):
    payload = {
        "email": email,
        "orderLines": [{
            "metadataUuid": DATASET_UUID,
            "areas": [{"code":"Kart","name":"Valgt fra kart","type":"polygon"}],
            "projections":[{"code":str(projection)}],
            "formats":[{"name":format_name}],
            "coordinates": coords_str,
            "coordinateSystem": coord_sys
        }]
    }
    r = requests.post(ORDER_URL, json=payload, timeout=60)
    if r.status_code != 200:
        raise Exception(f"Bestilling feilet: {r.status_code} {r.text[:300]}")
    return r.json().get("referenceNumber")

def poll_until_ready(ref, timeout_s=300, interval=6):
    end = time.time() + timeout_s
    while time.time() < end:
        r = requests.get(STATUS_URL.format(ref=ref), timeout=30)
        if r.status_code != 200:
            time.sleep(interval); continue
        info = r.json()
        files = info.get("files", [])
        ready = [f for f in files if f.get("status")=="ReadyForDownload" and f.get("downloadUrl")]
        if ready:
            return ready[0]["downloadUrl"]
        time.sleep(interval)
    raise Exception("Tidsavbrudd: nedlastingsfil ikke klar i tide.")

def geojson_to_gpx(geojson):
    gpx = gpxpy.gpx.GPX()
    for feat in geojson.get("features", []):
        geom = shape(feat["geometry"])
        polys = [geom] if isinstance(geom, Polygon) else list(geom.geoms) if isinstance(geom, MultiPolygon) else []
        for poly in polys:
            seg = gpxpy.gpx.GPXTrackSegment()
            for x, y in poly.exterior.coords:
                seg.points.append(gpxpy.gpx.GPXTrackPoint(y, x))
            gpx.tracks.append(gpxpy.gpx.GPXTrack(segments=[seg]))
    return gpx.to_xml()

def geojson_to_kml(geojson):
    kml_doc = kml.KML(); ns = "{http://www.opengis.net/kml/2.2}"
    doc = kml.Document(ns, "1", "Eiendomsteig", "Fra Geonorge (flislagt)"); kml_doc.append(doc)
    for feat in geojson.get("features", []):
        pm = kml.Placemark(ns, None, "Teig", ""); pm.geometry = shape(feat["geometry"]); doc.append(pm)
    return kml_doc.to_string(prettyprint=True)

def render_map(geojson):
    if not geojson.get("features"): return "<p>Ingen teiger i resultatet.</p>"
    geom0 = shape(geojson["features"][0]["geometry"])
    minx, miny, maxx, maxy = geom0.bounds
    m = folium.Map(location=[(miny+maxy)/2, (minx+maxx)/2], zoom_start=13)
    folium.GeoJson(geojson, name="Teiger").add_to(m)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".html").name
    m.save(tmp)
    with open(tmp, "r", encoding="utf-8") as f: return f.read()

@app.route("/", methods=["GET", "POST"])
def index():
    message = None; map_html = None; downloads = {}
    if request.method == "POST":
        email = request.form.get("email","").strip()
        adresse = request.form.get("adresse","").strip()
        kommunenr = request.form.get("kommunenr","").strip()
        gnr = request.form.get("gnr","").strip()
        bnr = request.form.get("bnr","").strip()
        fnr = request.form.get("fnr","").strip()
        snr = request.form.get("snr","").strip()
        radius_m = int(request.form.get("radius_m","5000"))
        tile_m = int(request.form.get("tile_m","2000"))
        try:
            if not email: return "E-post kreves for bestilling hos Geonorge."
            if not adresse: return "Oppgi en adresse nær midten av eiendommen."
            # Geokoding
            r = requests.get(ADRESSE_API, params={"sok": adresse, "treffPerSide": 1}, timeout=30)
            r.raise_for_status()
            data = r.json()
            if not data.get("adresser"): return "Fant ikke adressen i Geonorge."
            a = data["adresser"][0]
            if "representasjonspunkt" in a and a["representasjonspunkt"]:
                lon, lat = float(a["representasjonspunkt"]["lon"]), float(a["representasjonspunkt"]["lat"])
            else:
                epsg = int(a["punkt"]["epsg"]); E, N = [float(x) for x in a["punkt"]["koordinater"].split(",")]
                trans = Transformer.from_crs(epsg, 4326, always_xy=True); lon, lat = trans.transform(E, N)
            # Bygg rute og tiles
            to_utm = Transformer.from_crs(4326, 25833, always_xy=True)
            x, y = to_utm.transform(lon, lat)
            poly = Polygon([(x-radius_m, y-radius_m), (x+radius_m, y-radius_m), (x+radius_m, y+radius_m), (x-radius_m, y+radius_m), (x-radius_m, y-radius_m)])
            tiles = []
            minx, miny, maxx, maxy = poly.bounds
            xx = minx
            while xx < maxx:
                yy = miny; nx = min(xx+tile_m, maxx)
                while yy < maxy:
                    ny = min(yy+tile_m, maxy)
                    tpoly = Polygon([(xx,yy),(nx,yy),(nx,ny),(xx,ny),(xx,yy)])
                    inter = poly.intersection(tpoly)
                    if not inter.is_empty:
                        if isinstance(inter, Polygon): tiles.append(inter)
                        else:
                            for g in getattr(inter, "geoms", []):
                                if isinstance(g, Polygon): tiles.append(g)
                    yy = ny
                xx = nx
            # Bestill og last ned
            collected = []
            for t in tiles:
                coords = " ".join([f"{round(px,3)} {round(py,3)}" for (px, py) in list(t.exterior.coords)])
                ref = order_polygon(coords, "25833", email=email, format_name="GeoJSON", projection="4326")
                url = poll_until_ready(ref, timeout_s=300, interval=6)
                resp = requests.get(url, timeout=120); resp.raise_for_status()
                zf = zipfile.ZipFile(io.BytesIO(resp.content))
                for name in zf.namelist():
                    if name.lower().endswith((".geojson",".json")):
                        collected.append(zf.read(name)); break
            if not collected: return "Ingen data returnert. Øk radius eller tile-størrelse."
            merged = {"type":"FeatureCollection","features":[]}
            for b in collected:
                d = json.loads(b.decode("utf-8")); merged["features"].extend(d.get("features", []))
            # Filter (valgfritt)
            if any([kommunenr, gnr, bnr, fnr, snr]):
                feats = []
                for f in merged.get("features", []):
                    p = f.get("properties", {}) or {}
                    ok = True
                    if kommunenr and str(p.get("kommunenr")) != str(kommunenr): ok=False
                    if ok and gnr and str(p.get("gardsnr")) != str(gnr): ok=False
                    if ok and bnr and str(p.get("bruksnr")) != str(bnr): ok=False
                    if ok and fnr and str(p.get("festenr")) != str(fnr): ok=False
                    if ok and snr and str(p.get("seksjonsnr")) != str(snr): ok=False
                    if ok: feats.append(f)
                if not feats: return "Ingen teiger matchet filteret. Fjern filter og prøv igjen."
                merged = {"type":"FeatureCollection","features":feats}
            # Eksporter
            gpx = gpxpy.gpx.GPX()
            for feat in merged.get("features", []):
                geom = shape(feat["geometry"])
                polys = [geom] if isinstance(geom, Polygon) else list(geom.geoms) if isinstance(geom, MultiPolygon) else []
                for poly in polys:
                    seg = gpxpy.gpx.GPXTrackSegment()
                    for x0, y0 in poly.exterior.coords:
                        seg.points.append(gpxpy.gpx.GPXTrackPoint(y0, x0))
                    gpx.tracks.append(gpxpy.gpx.GPXTrack(segments=[seg]))
            gpx_xml = gpx.to_xml()
            kml_doc = kml.KML(); ns = "{http://www.opengis.net/kml/2.2}"
            doc = kml.Document(ns, "1", "Eiendomsteig", "Fra Geonorge (flislagt)"); kml_doc.append(doc)
            for feat in merged.get("features", []):
                pm = kml.Placemark(ns, None, "Teig", ""); pm.geometry = shape(feat["geometry"]); doc.append(pm)
            kml_xml = kml_doc.to_string(prettyprint=True)
            gp = tempfile.NamedTemporaryFile(delete=False, suffix=".gpx").name
            kp = tempfile.NamedTemporaryFile(delete=False, suffix=".kml").name
            open(gp,"w",encoding="utf-8").write(gpx_xml); open(kp,"w",encoding="utf-8").write(kml_xml)
            map_html = render_map(merged)
            downloads = {"gpx": f"/nedlast?path={gp}", "kml": f"/nedlast?path={kp}"}
            return render_template("index.html", map_html=map_html, downloads=downloads)
        except Exception as e:
            message = f"Feil: {e}"
    return render_template("index.html", message=message)

@app.route("/nedlast")
def nedlast():
    path = request.args.get("path")
    if not path or not os.path.exists(path): return "Filen finnes ikke lenger. Generer på nytt."
    return send_file(path, as_attachment=True)

if __name__ == "__main__":
    app.run(debug=True)