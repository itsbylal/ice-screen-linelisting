#!/usr/bin/env python3
"""
Build a self-contained ICE TB line-list webpage from the Azure JSONL export.

Source can be a local file or an Azure Blob / SAS URL:
    python build_linelist.py                        # use SOURCE below
    python build_linelist.py <path-or-url>          # override for one run

Safety behaviour on a refresh run:
  * the fetched payload must parse and pass sanity checks before anything is
    overwritten - a truncated or failed download leaves the existing page intact
  * every successful fetch is archived to snapshots/ so you can roll back
  * the HTML is written atomically (temp file + replace), never half-written
"""
import json, re, csv, io, os, sys, datetime, shutil, tempfile

HERE   = os.path.dirname(os.path.abspath(__file__))
# Works both flat (everything in one folder) and in the repo layout, where this
# script sits in build/ and the hosted page belongs in ../www/
ROOT   = os.path.dirname(HERE) if os.path.basename(HERE) == 'build' else HERE

CSVSRC = os.path.join(HERE, 'schema_header.csv')     # canonical 162-column order
SNAPS  = os.path.join(ROOT, 'snapshots')
LOG    = os.path.join(ROOT, 'refresh_log.txt')
URLFILE= os.path.join(ROOT, 'source_url.txt')        # holds the SAS URL, one line

ARGS   = [a for a in sys.argv[1:] if not a.startswith('--')]
FLAGS  = {a for a in sys.argv[1:] if a.startswith('--')}
# --hosted: build for Azure Static Web Apps. No records and no SAS token are
#           embedded; the page fetches /api/data, which the platform gates
#           behind Entra ID sign-in. Safe to commit to a repo.
HOSTED = '--hosted' in FLAGS
OUT    = os.path.join(ROOT, 'www', 'index.html') if HOSTED \
         else os.path.join(ROOT, 'ice_tb_line_list.html')

def configured_source():
    """Precedence: command-line arg > ICE_SOURCE env var > source_url.txt."""
    if ARGS:
        return ARGS[0]
    if os.environ.get('ICE_SOURCE'):
        return os.environ['ICE_SOURCE']
    if os.path.exists(URLFILE):
        for line in open(URLFILE, encoding='utf-8'):
            line = line.strip()
            if line and not line.startswith('#'):
                return line
    return ''

SOURCE = configured_source()

def live_url():
    """The URL baked into the page for its own live fetch. Independent of the
    source this run happened to read, so rebuilding from a local snapshot
    still produces a page that fetches live."""
    for cand in (os.environ.get('ICE_SOURCE', ''), SOURCE):
        if cand.startswith('http'):
            return cand
    if os.path.exists(URLFILE):
        for line in open(URLFILE, encoding='utf-8'):
            line = line.strip()
            if line.startswith('http'):
                return line
    return ''

LIVE_URL = live_url()

def newest_snapshot():
    """Fallback when no URL is configured: rebuild from the most recent archive."""
    if not os.path.isdir(SNAPS):
        return None
    files = sorted(f for f in os.listdir(SNAPS) if f.endswith('.json'))
    return os.path.join(SNAPS, files[-1]) if files else None

def log(msg):
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{stamp}] {msg}'
    print(line)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')

def check_sas_expiry(url):
    """Warn while there is still time to get a new token, not after it lapses."""
    m = re.search(r'[?&]se=([^&]+)', url)
    if not m:
        return
    from urllib.parse import unquote
    try:
        exp = datetime.datetime.strptime(
            unquote(m.group(1)).replace('Z', ''), '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return
    days = (exp - datetime.datetime.utcnow()).days
    if days < 0:
        log(f'SAS TOKEN EXPIRED on {exp:%Y-%m-%d} — request a new URL')
    elif days <= 30:
        log(f'WARNING: SAS token expires in {days} days ({exp:%Y-%m-%d}) — request a new URL soon')
    else:
        log(f'SAS token valid until {exp:%Y-%m-%d} ({days} days left)')

def read_source(src):
    """Return the raw JSONL text from a URL or a local path."""
    if src.startswith(('http://', 'https://')):
        check_sas_expiry(src)
        import urllib.request
        log(f'fetching {src.split("?")[0]} …')
        req = urllib.request.Request(src, headers={'User-Agent': 'ice-linelist/1.0'})
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read().decode('utf-8-sig')
        log(f'downloaded {len(raw):,} bytes')
        return raw
    # a relative path is resolved against the working directory first, then the
    # repo root, then this script's folder
    if os.path.isabs(src):
        path = src
    else:
        for base in (os.getcwd(), ROOT, HERE):
            cand = os.path.join(base, src)
            if os.path.exists(cand):
                path = cand
                break
        else:
            raise FileNotFoundError(f'{src} not found in {os.getcwd()}, {ROOT} or {HERE}')
    log(f'reading local file {path}')
    return open(path, encoding='utf-8-sig').read()

def parse(raw):
    """Accept JSONL (one object per line) or a single JSON array."""
    txt = raw.strip()
    if txt.startswith('['):
        out = json.loads(txt)
    else:
        out = [json.loads(l) for l in txt.splitlines() if l.strip()]
    if not isinstance(out, list) or not out or not isinstance(out[0], dict):
        raise ValueError('source did not contain a list of records')
    return out

# ---------------------------------------------------------------- load
if not SOURCE:
    SOURCE = newest_snapshot()
    if not SOURCE:
        raise SystemExit('No source configured. Set SOURCE to your SAS URL, or pass it as an argument.')
    log('no URL configured — rebuilding from the newest local snapshot')

try:
    raw  = read_source(SOURCE)
    recs = parse(raw)
except Exception as e:
    log(f'FETCH/PARSE FAILED: {type(e).__name__}: {e}')
    log('existing page left unchanged')
    raise SystemExit(1)
log(f'parsed {len(recs):,} records')

# sanity gate: refuse to rebuild from something that does not look like this dataset
MUST_HAVE = {'UID', 'Case_ID', 'CP_Province', 'CP_EventDate', 'MTB'}
missing = MUST_HAVE - set().union(*(o.keys() for o in recs))
if missing:
    raise SystemExit(f'ABORT: source is missing expected fields {sorted(missing)} — page left unchanged')
if len(recs) < 100:
    raise SystemExit(f'ABORT: only {len(recs)} records — looks truncated, page left unchanged')

# archive the good payload (skip when we just read from the archive itself)
snap = os.path.join(SNAPS, 'ice_data_' + datetime.date.today().isoformat() + '.json')
if os.path.abspath(SOURCE) != os.path.abspath(snap):
    os.makedirs(SNAPS, exist_ok=True)
    with open(snap, 'w', encoding='utf-8') as f:
        f.write(raw)
else:
    snap += '  (already archived)'

# canonical column order: the 162-column schema header, then any JSON-only extras
csv_header = next(csv.reader(open(CSVSRC, encoding='utf-8-sig')))
json_keys  = []
for o in recs:
    for k in o:
        if k not in json_keys:
            json_keys.append(k)

order = [c for c in csv_header if c in set(json_keys)]
order += [k for k in json_keys if k not in set(order)]

# front-load the identity columns the way the line list reads, admin fields last
# Refusal_Reasons sits immediately after the consent block
LEAD  = ['Username', 'UID', 'Consent_TongueSwab', 'Consent_Followup', 'Consent', 'Consent_Sign',
         'Refusal_Reasons']
ADMIN = ['archived', 'app_id', 'Form_Name', 'Case_ID']
cols  = [c for c in LEAD if c in order]
cols += [c for c in order if c not in set(LEAD) | set(ADMIN)]
cols += [c for c in ADMIN if c in order]

# ---------------------------------------------------------------- decode
CONFIRMED = {
    'MTB':            {'1': 'Detected', '2': 'Not Detected', '3': 'Invalid', '4': 'Error'},
    'MTBDetected':    {'1': 'High', '2': 'Medium', '3': 'Low', '4': 'Very Low', '5': 'Trace'},
    'RR':             {'1': 'Detected', '2': 'Not Detected', '3': 'Indeterminate'},
    'XpertPerformed': {'1': 'Yes', '2': 'No'},
    'PI_Gender':      {'1': 'Male', '2': 'Female', '3': 'Other'},
}
PROVINCE = {
    'khyber_pakhtunkhwa': 'KPK', 'sindh': 'Sindh', 'balochistan': 'Balochistan',
    'ict': 'ICT', 'ajk': 'AJK', 'punjab': 'Punjab', 'gb': 'GB',
}
LAB = {
    'national_reference_lab_nrl_islamabad': 'NRL Islamabad',
    'prl_sindh_karachi_dow_university_of_health_sciences_duhs-karachi': 'PRL Sindh — DUHS Karachi',
}
def cell(col, raw):
    # Match JavaScript's Number->String rules so a live fetch in the browser and
    # a rebuild by this script render numbers identically (5.0 -> "5", not "5.0")
    if isinstance(raw, float):
        raw = int(raw) if raw.is_integer() else raw
    v = '' if raw is None else str(raw).strip()
    if v == '':
        return ''
    if col in CONFIRMED:
        return CONFIRMED[col].get(v, v)
    if col == 'CP_Province':
        return PROVINCE.get(v.lower(), v)
    if col == 'LaboratoryName':
        return LAB.get(v.lower(), v)
    if col == 'archived':
        return 'Yes' if v.lower() in ('true', '1') else 'No'
    return v

rows = [[cell(c, o.get(c)) for c in cols] for o in recs]

# ---------------------------------------------------------------- labels
OVERRIDE = {
    'Username': 'Field User', 'UID': 'UID', 'app_id': 'App ID', 'Case_ID': 'Case ID',
    'Form_Name': 'Form Name', 'archived': 'Archived',
    'Consent_TongueSwab': 'Consent (Tongue Swab)', 'Consent_Followup': 'Consent (Follow-up)',
    'Consent': 'Consent', 'Consent_Sign': 'Consent Sign',
    'CP_Province': 'Province', 'CP_District': 'District', 'CP_SR': 'SR', 'CP_Van': 'Van',
    'CP_TehsilVillage': 'Tehsil/Village', 'CP_CampVenue': 'Camp/Venue',
    'CP_EventDate': 'Event Date', 'CP_EventID': 'Event ID',
    'MTB': 'MTB Result', 'MTBDetected': 'MTB Grade', 'RR': 'RR (Rifampicin)',
    'MTBErrorCode': 'MTB Error Code', 'BMI': 'BMI', 'Age_Group': 'Age Group',
    'CX_CAD4TBScore': 'CAD4TB Score', 'CX_DicomNumber': 'DICOM Number',
    'ElectronicLabRegNo': 'Electronic Lab Reg No', 'Refusal_Reasons': 'Refusal Reasons',
}
PREFIX = {
    'CP_': 'Camp', 'PI_': '', 'RF_': 'Risk', 'VS_': 'Symptom', 'GQ_': 'Pref',
    'PS_': 'Pre-sample', 'CX_': 'X-ray', 'CS_': 'Cough', 'SC_': 'Collection',
    'SSC_': 'Satisfaction',
}
def prettify(c):
    if c in OVERRIDE:
        return OVERRIDE[c]
    s = c
    for p in sorted(PREFIX, key=len, reverse=True):
        if s.startswith(p):
            s = s[len(p):]
            break
    s = s.replace('_', ' ')
    s = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', s)
    s = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', s)
    s = re.sub(r'\((\w+)\s+(\w+)\)', r'(\1 \2)', s)
    return s.strip()

labels = [prettify(c) for c in cols]

# ---------------------------------------------------------------- groups
def group_of(c):
    if c in ('Username', 'UID', 'Case_ID', 'app_id', 'Form_Name', 'archived'):
        return 'Record'
    if c.startswith('Consent') or c.startswith('Refusal'):
        return 'Consent'
    if c.startswith('CP_'):
        return 'Camp'
    if c.startswith(('PI_', 'Age_Group', 'Education')):
        return 'Participant'
    if c.startswith('RF_'):
        return 'Risk factors'
    if c.startswith('VS_'):
        return 'Symptoms'
    if c.startswith(('GQ_', 'PS_', 'SSC_')):
        return 'Experience'
    if c.startswith(('CX_', 'BMI', 'CS_')):
        return 'X-ray / vitals'
    if c.startswith(('SC_',)):
        return 'Collection'
    if c.startswith(('TS', 'SS')) or c in ('LaboratoryName', 'ElectronicLabRegNo', 'SpecimenReceivingDate'):
        return 'Lab samples'
    if c.startswith(('Xpert', 'MTB', 'RR')):
        return 'Xpert'
    if c.startswith('Microscopy'):
        return 'Microscopy'
    if c.startswith(('MGIT', 'LJ', 'Culture')):
        return 'Culture'
    if c.startswith('Discordance'):
        return 'Discordance'
    return 'Other'

groups = [group_of(c) for c in cols]

# ---------------------------------------------------------------- filter domains
def domain(col):
    vals = sorted({r[cols.index(col)] for r in rows} - {''})
    return vals

# per-column header filters: dropdown where the column is low-cardinality,
# free-text "contains" box everywhere else
DOMAIN_MAX = 60
domains = {}
for i, c in enumerate(cols):
    vals = sorted({r[i] for r in rows} - {''})
    if 0 < len(vals) <= DOMAIN_MAX:
        domains[c] = vals

if HOSTED:
    # schema only: column order, labels and grouping. Filter values and every
    # patient record are fetched at run time from /api/data after sign-in.
    rows = []
    domains = {}

payload = {
    'gate': not HOSTED,          # local file keeps the code gate; hosted uses Entra ID
    'cols': cols,
    'labels': labels,
    'groups': groups,
    'rows': rows,
    'domains': domains,
    # everything the page needs to re-derive the table from a live fetch,
    # so the browser reproduces exactly what this script does
    'live': {
        # hosted build calls the authenticated proxy; the token stays server-side
        'url': '/api/data' if HOSTED else LIVE_URL,
        'decode': CONFIRMED,
        'province': PROVINCE,
        'lab': LAB,
        'domainMax': DOMAIN_MAX,
        'filterCols': ['CP_Province', 'CP_District', 'Username',
                       'LaboratoryName', 'MTB', 'RR'],
    },
    'meta': {
        'records': len(rows),
        'variables': len(cols),
        'built': datetime.datetime.now().strftime('%d %B %Y - %H:%M'),
        'eventMin': '' if HOSTED else min(domain('CP_EventDate')),
        'eventMax': '' if HOSTED else max(domain('CP_EventDate')),
    },
    # order here drives the toolbar order in the page
    'filters': {c: ([] if HOSTED else domain(c)) for c in
                ['CP_Province', 'CP_District', 'Username',
                 'LaboratoryName', 'MTB', 'RR']},
}

tpl = open(os.path.join(HERE, 'linelist_template.html'), encoding='utf-8').read()
log(('hosted' if HOSTED else 'local') + ' build -> ' + OUT)
data_js = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
out = tpl.replace('/*__DATA__*/null', data_js)
if '/*__DATA__*/' in out:
    raise SystemExit('ABORT: data placeholder not replaced — page left unchanged')

# atomic write: build beside the target, then swap in, so a crash mid-write
# can never leave a half-written page behind
os.makedirs(os.path.dirname(OUT), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT), suffix='.tmp')
with os.fdopen(fd, 'w', encoding='utf-8') as f:
    f.write(out)
os.replace(tmp, OUT)

log(f'built {len(rows):,} records x {len(cols)} variables '
    f'({round(os.path.getsize(OUT)/1e6, 2)} MB), events to {payload["meta"]["eventMax"]}')
print('dropdown filter cols:', len(domains), '| text filter cols:', len(cols) - len(domains))
print('snapshot  :', snap)
