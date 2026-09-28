"""
UGD — Appel & présences aux cours
Flask + SQLite. Entraîneurs : accès par code de cours. Bureau : mot de passe (ADMIN_PASSWORD).
"""
import os, csv, json, sqlite3, secrets, unicodedata, re
from io import BytesIO, StringIO
from datetime import date, datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for, flash,
                   session, abort, send_file)
from openpyxl import load_workbook, Workbook
from openpyxl.styles import Font, PatternFill, Alignment

DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DATA_DIR, 'presences.db')
IMPORT_TMP = os.path.join(DATA_DIR, '_import_tmp.json')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'ugd2026')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-moi-en-production')
app.permanent_session_lifetime = timedelta(days=180)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

JOURS = ['Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi', 'Dimanche']
STATUTS = {'P': 'Présent', 'A': 'Absent', 'E': 'Excusé'}

# ── BASE DE DONNÉES ──────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = get_db()
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS cours (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT NOT NULL UNIQUE,
        jour TEXT DEFAULT '', horaire TEXT DEFAULT '', salle TEXT DEFAULT '',
        entraineur TEXT DEFAULT '',
        code TEXT NOT NULL UNIQUE,
        actif INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS gymnastes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cle TEXT NOT NULL UNIQUE,
        nom TEXT NOT NULL, prenom TEXT DEFAULT '', naissance TEXT DEFAULT '',
        actif INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS inscriptions (
        gymnaste_id INTEGER REFERENCES gymnastes(id) ON DELETE CASCADE,
        cours_id INTEGER REFERENCES cours(id) ON DELETE CASCADE,
        PRIMARY KEY (gymnaste_id, cours_id)
    );
    CREATE TABLE IF NOT EXISTS seances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cours_id INTEGER REFERENCES cours(id) ON DELETE CASCADE,
        date TEXT NOT NULL,
        saisie_le TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE (cours_id, date)
    );
    CREATE TABLE IF NOT EXISTS presences (
        seance_id INTEGER REFERENCES seances(id) ON DELETE CASCADE,
        gymnaste_id INTEGER REFERENCES gymnastes(id) ON DELETE CASCADE,
        statut TEXT NOT NULL CHECK (statut IN ('P','A','E')),
        PRIMARY KEY (seance_id, gymnaste_id)
    );
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    ''')
    conn.commit(); conn.close()

def get_setting(key, default=None):
    conn = get_db()
    r = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    conn.close()
    return r['value'] if r else default

def set_setting(key, value):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)', (key, value))
    conn.commit(); conn.close()

def seuil_alerte():
    try: return max(1, int(get_setting('seuil_alerte', '3')))
    except ValueError: return 3

# ── OUTILS ───────────────────────────────────────────────────────────────────
def norm(s):
    s = unicodedata.normalize('NFKD', str(s or '')).encode('ascii', 'ignore').decode()
    return re.sub(r'\s+', ' ', s).strip().lower()

def nouveau_code(conn):
    while True:
        code = f'{secrets.randbelow(10000):04d}'
        if not conn.execute('SELECT 1 FROM cours WHERE code=?', (code,)).fetchone():
            return code

def fmt_date(d):
    try:
        dt = datetime.strptime(d, '%Y-%m-%d')
        return f"{JOURS[dt.weekday()]} {dt.day:02d}/{dt.month:02d}/{dt.year}"
    except Exception:
        return d
app.jinja_env.filters['fdate'] = fmt_date
app.jinja_env.globals['STATUTS'] = STATUTS

def is_admin():
    return session.get('admin') is True
app.jinja_env.globals['is_admin'] = is_admin

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not is_admin():
            return redirect(url_for('admin_login', next=request.path))
        return f(*a, **k)
    return w

def cours_autorise(cours_id):
    return is_admin() or cours_id in session.get('cours', [])

def stats_gymnaste_cours(conn, gid, cid):
    rows = conn.execute('''SELECT p.statut FROM presences p JOIN seances s ON s.id=p.seance_id
                           WHERE p.gymnaste_id=? AND s.cours_id=? ORDER BY s.date''', (gid, cid)).fetchall()
    st = [r['statut'] for r in rows]
    n = len(st)
    return {'n': n, 'P': st.count('P'), 'A': st.count('A'), 'E': st.count('E'),
            'taux': round(100 * st.count('P') / n) if n else None}

def alertes(conn, cours_id=None):
    """Gymnastes avec N absences NON excusées consécutives (les excusés sont ignorés)
    sur leurs dernières séances d'un cours."""
    seuil = seuil_alerte()
    sql = '''SELECT i.gymnaste_id, i.cours_id, g.nom, g.prenom, c.nom cours_nom
             FROM inscriptions i JOIN gymnastes g ON g.id=i.gymnaste_id JOIN cours c ON c.id=i.cours_id
             WHERE g.actif=1 AND c.actif=1'''
    params = []
    if cours_id:
        sql += ' AND i.cours_id=?'; params.append(cours_id)
    res = []
    for r in conn.execute(sql, params).fetchall():
        sts = conn.execute('''SELECT p.statut FROM presences p JOIN seances s ON s.id=p.seance_id
                              WHERE p.gymnaste_id=? AND s.cours_id=? AND p.statut!='E'
                              ORDER BY s.date DESC LIMIT ?''', (r['gymnaste_id'], r['cours_id'], seuil)).fetchall()
        if len(sts) == seuil and all(s['statut'] == 'A' for s in sts):
            res.append(r)
    return res

# ── ENTRAÎNEURS ──────────────────────────────────────────────────────────────
@app.route('/')
def accueil():
    ids = session.get('cours', [])
    conn = get_db()
    mes_cours = []
    if ids:
        q = ','.join('?' * len(ids))
        mes_cours = conn.execute(f'SELECT * FROM cours WHERE id IN ({q}) AND actif=1 ORDER BY nom', ids).fetchall()
    conn.close()
    return render_template('accueil.html', mes_cours=mes_cours)

@app.route('/code', methods=['POST'])
def saisir_code():
    code = request.form.get('code', '').strip()
    conn = get_db()
    c = conn.execute('SELECT * FROM cours WHERE code=? AND actif=1', (code,)).fetchone()
    conn.close()
    if not c:
        flash('Code inconnu. Vérifie le code du cours auprès du bureau.', 'danger')
        return redirect(url_for('accueil'))
    session.permanent = True
    ids = session.get('cours', [])
    if c['id'] not in ids:
        ids.append(c['id'])
    session['cours'] = ids
    return redirect(url_for('cours_detail', id=c['id']))

@app.route('/cours/<int:id>/oublier', methods=['POST'])
def oublier_cours(id):
    session['cours'] = [i for i in session.get('cours', []) if i != id]
    return redirect(url_for('accueil'))

@app.route('/cours/<int:id>')
def cours_detail(id):
    if not cours_autorise(id): abort(403)
    conn = get_db()
    c = conn.execute('SELECT * FROM cours WHERE id=?', (id,)).fetchone()
    if not c: conn.close(); abort(404)
    seances = conn.execute('''SELECT s.*,
            SUM(p.statut='P') np, SUM(p.statut='A') na, SUM(p.statut='E') ne
            FROM seances s LEFT JOIN presences p ON p.seance_id=s.id
            WHERE s.cours_id=? GROUP BY s.id ORDER BY s.date DESC''', (id,)).fetchall()
    gyms = conn.execute('''SELECT g.* FROM gymnastes g JOIN inscriptions i ON i.gymnaste_id=g.id
            WHERE i.cours_id=? AND g.actif=1 ORDER BY g.nom, g.prenom''', (id,)).fetchall()
    stats = [(g, stats_gymnaste_cours(conn, g['id'], id)) for g in gyms]
    al = alertes(conn, id)
    conn.close()
    return render_template('cours.html', c=c, seances=seances, stats=stats, alertes=al,
                           aujourdhui=date.today().isoformat(), seuil=seuil_alerte())

@app.route('/cours/<int:id>/appel', methods=['GET', 'POST'])
def appel(id):
    if not cours_autorise(id): abort(403)
    d = request.values.get('date') or date.today().isoformat()
    try: datetime.strptime(d, '%Y-%m-%d')
    except ValueError: abort(400)
    conn = get_db()
    c = conn.execute('SELECT * FROM cours WHERE id=?', (id,)).fetchone()
    if not c: conn.close(); abort(404)
    gyms = conn.execute('''SELECT g.* FROM gymnastes g JOIN inscriptions i ON i.gymnaste_id=g.id
            WHERE i.cours_id=? AND g.actif=1 ORDER BY g.nom, g.prenom''', (id,)).fetchall()
    seance = conn.execute('SELECT * FROM seances WHERE cours_id=? AND date=?', (id, d)).fetchone()

    if request.method == 'POST':
        if not seance:
            conn.execute('INSERT INTO seances (cours_id, date) VALUES (?,?)', (id, d))
            seance = conn.execute('SELECT * FROM seances WHERE cours_id=? AND date=?', (id, d)).fetchone()
        else:
            conn.execute("UPDATE seances SET saisie_le=datetime('now','localtime') WHERE id=?", (seance['id'],))
        for g in gyms:
            st = request.form.get(f"g{g['id']}", 'P')
            if st not in STATUTS: st = 'P'
            conn.execute('INSERT OR REPLACE INTO presences (seance_id,gymnaste_id,statut) VALUES (?,?,?)',
                         (seance['id'], g['id'], st))
        conn.commit(); conn.close()
        flash(f'Appel enregistré pour le {fmt_date(d)}.', 'success')
        return redirect(url_for('cours_detail', id=id))

    existants = {}
    if seance:
        for r in conn.execute('SELECT gymnaste_id, statut FROM presences WHERE seance_id=?', (seance['id'],)):
            existants[r['gymnaste_id']] = r['statut']
    conn.close()
    return render_template('appel.html', c=c, gyms=gyms, d=d, existants=existants, deja=bool(seance))

@app.route('/cours/<int:id>/seance/<int:sid>/supprimer', methods=['POST'])
def seance_supprimer(id, sid):
    if not cours_autorise(id): abort(403)
    conn = get_db()
    conn.execute('DELETE FROM seances WHERE id=? AND cours_id=?', (sid, id))
    conn.commit(); conn.close()
    flash('Séance supprimée.', 'success')
    return redirect(url_for('cours_detail', id=id))

# ── BUREAU ───────────────────────────────────────────────────────────────────
@app.route('/bureau/connexion', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if secrets.compare_digest(request.form.get('password', ''), ADMIN_PASSWORD):
            session.permanent = True
            session['admin'] = True
            nxt = request.args.get('next', '')
            return redirect(nxt if nxt.startswith('/') else url_for('admin'))
        flash('Mot de passe incorrect.', 'danger')
    return render_template('login.html')

@app.route('/bureau/deconnexion')
def admin_logout():
    session.pop('admin', None)
    return redirect(url_for('accueil'))

@app.route('/bureau')
@admin_required
def admin():
    conn = get_db()
    cours = conn.execute('''SELECT c.*,
        (SELECT COUNT(*) FROM inscriptions i JOIN gymnastes g ON g.id=i.gymnaste_id WHERE i.cours_id=c.id AND g.actif=1) nb_gym,
        (SELECT COUNT(*) FROM seances s WHERE s.cours_id=c.id) nb_seances,
        (SELECT MAX(date) FROM seances s WHERE s.cours_id=c.id) derniere,
        (SELECT ROUND(100.0*SUM(p.statut='P')/COUNT(*)) FROM presences p JOIN seances s ON s.id=p.seance_id WHERE s.cours_id=c.id) taux
        FROM cours c ORDER BY c.actif DESC,
        CASE c.jour WHEN 'Lundi' THEN 1 WHEN 'Mardi' THEN 2 WHEN 'Mercredi' THEN 3 WHEN 'Jeudi' THEN 4
        WHEN 'Vendredi' THEN 5 WHEN 'Samedi' THEN 6 WHEN 'Dimanche' THEN 7 ELSE 8 END, c.horaire, c.nom''').fetchall()
    nb_gym = conn.execute('SELECT COUNT(*) FROM gymnastes WHERE actif=1').fetchone()[0]
    al = alertes(conn)
    conn.close()
    return render_template('admin.html', cours=cours, nb_gym=nb_gym, alertes=al, seuil=seuil_alerte())

@app.route('/bureau/reglages', methods=['POST'])
@admin_required
def admin_reglages():
    set_setting('seuil_alerte', str(max(1, int(request.form.get('seuil_alerte', 3) or 3))))
    flash('Réglage enregistré.', 'success')
    return redirect(url_for('admin'))

@app.route('/bureau/cours/nouveau', methods=['GET', 'POST'])
@app.route('/bureau/cours/<int:id>', methods=['GET', 'POST'])
@admin_required
def admin_cours(id=None):
    conn = get_db()
    c = conn.execute('SELECT * FROM cours WHERE id=?', (id,)).fetchone() if id else None
    if id and not c: conn.close(); abort(404)
    if request.method == 'POST':
        f = request.form
        nom = f.get('nom', '').strip()
        code = f.get('code', '').strip() or nouveau_code(conn)
        dup = conn.execute('SELECT id FROM cours WHERE code=? AND id!=?', (code, id or 0)).fetchone()
        if not nom or dup:
            flash('Nom obligatoire.' if not nom else 'Ce code est déjà utilisé par un autre cours.', 'danger')
            conn.close()
            return render_template('cours_form.html', c=c, form=f, jours=JOURS)
        vals = (nom, f.get('jour', ''), f.get('horaire', '').strip(), f.get('salle', '').strip(),
                f.get('entraineur', '').strip(), code, 1 if f.get('actif') else 0)
        try:
            if c:
                conn.execute('UPDATE cours SET nom=?,jour=?,horaire=?,salle=?,entraineur=?,code=?,actif=? WHERE id=?', vals + (id,))
            else:
                conn.execute('INSERT INTO cours (nom,jour,horaire,salle,entraineur,code,actif) VALUES (?,?,?,?,?,?,?)', vals)
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            flash('Un cours porte déjà ce nom.', 'danger')
            return render_template('cours_form.html', c=c, form=f, jours=JOURS)
        conn.close()
        flash('Cours enregistré.', 'success')
        return redirect(url_for('admin'))
    form = dict(c) if c else {'actif': 1, 'code': nouveau_code(conn)}
    conn.close()
    return render_template('cours_form.html', c=c, form=form, jours=JOURS)

@app.route('/bureau/cours/<int:id>/supprimer', methods=['POST'])
@admin_required
def admin_cours_supprimer(id):
    conn = get_db()
    conn.execute('DELETE FROM cours WHERE id=?', (id,))
    conn.commit(); conn.close()
    flash('Cours supprimé, avec son historique de présences.', 'success')
    return redirect(url_for('admin'))

@app.route('/bureau/gymnastes')
@admin_required
def admin_gymnastes():
    q = request.args.get('q', '').strip()
    conn = get_db()
    sql = '''SELECT g.*, GROUP_CONCAT(c.nom, ', ') cours_noms,
             (SELECT COUNT(*) FROM presences p WHERE p.gymnaste_id=g.id) n,
             (SELECT SUM(p.statut='P') FROM presences p WHERE p.gymnaste_id=g.id) np
             FROM gymnastes g LEFT JOIN inscriptions i ON i.gymnaste_id=g.id LEFT JOIN cours c ON c.id=i.cours_id
             WHERE 1=1'''
    params = []
    if q:
        sql += ' AND (g.nom LIKE ? OR g.prenom LIKE ?)'; params += [f'%{q}%', f'%{q}%']
    sql += ' GROUP BY g.id ORDER BY g.actif DESC, g.nom, g.prenom'
    gyms = conn.execute(sql, params).fetchall()
    conn.close()
    return render_template('gymnastes.html', gyms=gyms, q=q)

@app.route('/bureau/gymnastes/<int:id>')
@admin_required
def admin_gymnaste(id):
    conn = get_db()
    g = conn.execute('SELECT * FROM gymnastes WHERE id=?', (id,)).fetchone()
    if not g: conn.close(); abort(404)
    cours = conn.execute('''SELECT c.* FROM cours c JOIN inscriptions i ON i.cours_id=c.id
                            WHERE i.gymnaste_id=? ORDER BY c.nom''', (id,)).fetchall()
    blocs = []
    for c in cours:
        hist = conn.execute('''SELECT s.date, p.statut FROM seances s
                               LEFT JOIN presences p ON p.seance_id=s.id AND p.gymnaste_id=?
                               WHERE s.cours_id=? ORDER BY s.date DESC''', (id, c['id'])).fetchall()
        blocs.append((c, stats_gymnaste_cours(conn, id, c['id']), hist))
    conn.close()
    return render_template('gymnaste.html', g=g, blocs=blocs)

# ── IMPORT KALISPORT ─────────────────────────────────────────────────────────
def lire_fichier(fs):
    nom = (fs.filename or '').lower()
    data = fs.read()
    if nom.endswith(('.xlsx', '.xlsm')):
        wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = [[('' if v is None else (v.strftime('%d/%m/%Y') if isinstance(v, (datetime, date)) else str(v).strip()))
                 for v in r] for r in ws.iter_rows(values_only=True)]
    elif nom.endswith(('.csv', '.txt')):
        try: txt = data.decode('utf-8-sig')
        except UnicodeDecodeError: txt = data.decode('latin-1')
        try: dialect = csv.Sniffer().sniff(txt[:4000], delimiters=';,\t')
        except csv.Error: dialect = csv.excel; dialect.delimiter = ';'
        rows = [[v.strip() for v in r] for r in csv.reader(StringIO(txt), dialect)]
    else:
        raise ValueError('Format non pris en charge : envoie un fichier .xlsx ou .csv.')
    rows = [r for r in rows if any(r)]
    if len(rows) < 2:
        raise ValueError('Le fichier ne contient pas de données.')
    # ligne d'en-têtes = première ligne avec au moins 2 cellules remplies
    h = next(i for i, r in enumerate(rows) if sum(1 for v in r if v) >= 2)
    headers = [v or f'Colonne {i+1}' for i, v in enumerate(rows[h])]
    body = [r + [''] * (len(headers) - len(r)) for r in rows[h + 1:]]
    return headers, body

def deviner(headers, mots, exclure=()):
    for i, h in enumerate(headers):
        n = norm(h)
        if any(m in n for m in mots) and not any(e in n for e in exclure):
            return i
    return None

@app.route('/bureau/import', methods=['GET', 'POST'])
@admin_required
def admin_import():
    if request.method == 'POST':
        fs = request.files.get('fichier')
        if not fs or not fs.filename:
            flash('Choisis un fichier à importer.', 'warning')
            return redirect(url_for('admin_import'))
        try:
            headers, body = lire_fichier(fs)
        except Exception as e:
            flash(f'Lecture impossible : {e}', 'danger')
            return redirect(url_for('admin_import'))
        with open(IMPORT_TMP, 'w', encoding='utf-8') as f:
            json.dump({'headers': headers, 'rows': body}, f)
        saved = json.loads(get_setting('mapping', '{}'))
        def pick(key, guess):
            v = saved.get(key)
            return headers.index(v) if v in headers else guess
        mapping = {
            'nom': pick('nom', deviner(headers, ['nom'], ['prenom', 'club', 'cours', 'groupe'])),
            'prenom': pick('prenom', deviner(headers, ['prenom'])),
            'naissance': pick('naissance', deviner(headers, ['naissance', 'date de n', 'ne le', 'né'])),
            'cours': pick('cours', deviner(headers, ['cours', 'groupe', 'section', 'activite', 'creneau'])),
        }
        return render_template('import_mapping.html', headers=headers, apercu=body[:5], mapping=mapping, total=len(body))
    return render_template('import.html')

@app.route('/bureau/import/valider', methods=['POST'])
@admin_required
def admin_import_valider():
    if not os.path.exists(IMPORT_TMP):
        flash("Le fichier importé a expiré, recommence l'import.", 'warning')
        return redirect(url_for('admin_import'))
    with open(IMPORT_TMP, encoding='utf-8') as f:
        tmp = json.load(f)
    headers, rows = tmp['headers'], tmp['rows']
    def col(k):
        v = request.form.get(k, '')
        return int(v) if v.isdigit() else None
    cn, cp, cd, cc = col('nom'), col('prenom'), col('naissance'), col('cours')
    if cn is None or cc is None:
        flash('Les colonnes « Nom » et « Cours » sont obligatoires.', 'danger')
        return redirect(url_for('admin_import'))
    set_setting('mapping', json.dumps({k: headers[v] for k, v in
                (('nom', cn), ('prenom', cp), ('naissance', cd), ('cours', cc)) if v is not None}))
    desactiver = bool(request.form.get('desactiver_absents'))

    conn = get_db()
    vus, nb_new, nb_cours_new = set(), 0, 0
    inscr = {}  # gymnaste_id -> set(cours_id)
    for r in rows:
        nom = r[cn].strip() if cn < len(r) else ''
        if not nom: continue
        prenom = r[cp].strip() if cp is not None else ''
        naiss = r[cd].strip() if cd is not None else ''
        cle = '|'.join([norm(nom), norm(prenom), norm(naiss)])
        g = conn.execute('SELECT id FROM gymnastes WHERE cle=?', (cle,)).fetchone()
        if g:
            gid = g['id']
            conn.execute('UPDATE gymnastes SET nom=?, prenom=?, naissance=?, actif=1 WHERE id=?', (nom, prenom, naiss, gid))
        else:
            conn.execute('INSERT INTO gymnastes (cle,nom,prenom,naissance) VALUES (?,?,?,?)', (cle, nom, prenom, naiss))
            gid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]; nb_new += 1
        vus.add(gid)
        # une cellule peut contenir plusieurs cours séparés par ; ou un retour à la ligne
        for nom_cours in re.split(r'[;\n]', r[cc] if cc < len(r) else ''):
            nom_cours = nom_cours.strip()
            if not nom_cours: continue
            c = conn.execute('SELECT id FROM cours WHERE nom=?', (nom_cours,)).fetchone()
            if not c:
                conn.execute('INSERT INTO cours (nom, code) VALUES (?,?)', (nom_cours, nouveau_code(conn)))
                c = conn.execute('SELECT id FROM cours WHERE nom=?', (nom_cours,)).fetchone(); nb_cours_new += 1
            inscr.setdefault(gid, set()).add(c['id'])
    for gid in vus:  # les inscriptions suivent le fichier Kalisport
        conn.execute('DELETE FROM inscriptions WHERE gymnaste_id=?', (gid,))
        for cid in inscr.get(gid, ()):
            conn.execute('INSERT OR IGNORE INTO inscriptions VALUES (?,?)', (gid, cid))
    nb_off = 0
    if desactiver and vus:
        q = ','.join('?' * len(vus))
        nb_off = conn.execute(f'UPDATE gymnastes SET actif=0 WHERE actif=1 AND id NOT IN ({q})', list(vus)).rowcount
    conn.commit(); conn.close()
    os.remove(IMPORT_TMP)
    msg = f'Import terminé : {len(vus)} gymnastes ({nb_new} nouveaux), {nb_cours_new} cours créés'
    if nb_off: msg += f', {nb_off} gymnastes absents du fichier passés inactifs'
    flash(msg + '.', 'success')
    return redirect(url_for('admin'))

# ── EXPORT EXCEL ─────────────────────────────────────────────────────────────
@app.route('/bureau/export')
@admin_required
def admin_export():
    conn = get_db()
    wb = Workbook()
    ws = wb.active; ws.title = 'Synthèse'
    bold = Font(bold=True, color='FFFFFF')
    fill = PatternFill('solid', fgColor='7A1F3D')
    ws.append(['Cours', 'Nom', 'Prénom', 'Séances', 'Présent', 'Absent', 'Excusé', 'Taux de présence'])
    for cell in ws[1]: cell.font = bold; cell.fill = fill
    cours = conn.execute('SELECT * FROM cours ORDER BY nom').fetchall()
    used = set()
    for c in cours:
        gyms = conn.execute('''SELECT g.* FROM gymnastes g JOIN inscriptions i ON i.gymnaste_id=g.id
                               WHERE i.cours_id=? ORDER BY g.nom, g.prenom''', (c['id'],)).fetchall()
        for g in gyms:
            s = stats_gymnaste_cours(conn, g['id'], c['id'])
            ws.append([c['nom'], g['nom'], g['prenom'], s['n'], s['P'], s['A'], s['E'],
                       (s['taux'] / 100) if s['taux'] is not None else None])
            ws.cell(ws.max_row, 8).number_format = '0%'
        # une feuille par cours : gymnastes × séances
        titre = re.sub(r'[\[\]\*\?/\\:]', '', c['nom'])[:28] or f"Cours {c['id']}"
        base, k = titre, 2
        while titre in used: titre = f'{base[:26]}{k}'; k += 1
        used.add(titre)
        wc = wb.create_sheet(titre)
        seances = conn.execute('SELECT * FROM seances WHERE cours_id=? ORDER BY date', (c['id'],)).fetchall()
        wc.append(['Nom', 'Prénom'] + [datetime.strptime(s['date'], '%Y-%m-%d').strftime('%d/%m') for s in seances])
        for cell in wc[1]: cell.font = bold; cell.fill = fill; cell.alignment = Alignment(horizontal='center')
        for g in gyms:
            m = {r['seance_id']: r['statut'] for r in conn.execute(
                'SELECT seance_id, statut FROM presences WHERE gymnaste_id=?', (g['id'],))}
            wc.append([g['nom'], g['prenom']] + [m.get(s['id'], '') for s in seances])
        wc.column_dimensions['A'].width = 20; wc.column_dimensions['B'].width = 16
    for col, w in zip('ABCDEFGH', (24, 20, 16, 10, 10, 10, 10, 16)):
        ws.column_dimensions[col].width = w
    conn.close()
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f'presences_UGD_{date.today().isoformat()}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.errorhandler(403)
def e403(e):
    return render_template('erreur.html', titre='Accès refusé',
                           texte='Saisis le code du cours sur la page d’accueil pour y accéder.'), 403

@app.errorhandler(404)
def e404(e):
    return render_template('erreur.html', titre='Page introuvable', texte='Cette page n’existe pas ou plus.'), 404

init_db()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
