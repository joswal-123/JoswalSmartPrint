import os, sqlite3, uuid, hmac, hashlib, json, math
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps
from flask import Flask, request, render_template, jsonify, send_file, abort
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()
BASE = Path(__file__).resolve().parent
UPLOAD_DIR = Path(os.getenv('UPLOAD_DIR', BASE / 'uploads'))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv('DATABASE_PATH', BASE / 'joswal_smart_print.db'))
ALLOWED = {'.pdf', '.jpg', '.jpeg', '.png'}
MAX_BYTES = int(os.getenv('MAX_UPLOAD_MB', '20')) * 1024 * 1024
PRICE_BW = int(os.getenv('PRICE_BW_PER_PAGE', '3'))
PRICE_COLOR = int(os.getenv('PRICE_COLOR_PER_PAGE', '10'))
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID', '').strip()
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET', '').strip()
RAZORPAY_WEBHOOK_SECRET = os.getenv('RAZORPAY_WEBHOOK_SECRET', '').strip()
PRINT_AGENT_TOKEN = os.getenv('PRINT_AGENT_TOKEN', '').strip()
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', '').rstrip('/')

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_BYTES
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'change-this-before-deploying')


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, original_name TEXT NOT NULL, stored_name TEXT NOT NULL,
            file_ext TEXT NOT NULL, pages INTEGER NOT NULL, copies INTEGER NOT NULL,
            color_mode TEXT NOT NULL, amount_paise INTEGER NOT NULL, status TEXT NOT NULL,
            razorpay_order_id TEXT, razorpay_payment_id TEXT, created_at TEXT NOT NULL,
            printed_at TEXT, error TEXT
        )''')


def get_pdf_pages(path: Path) -> int:
    try:
        from pypdf import PdfReader
        return max(1, len(PdfReader(str(path)).pages))
    except Exception:
        return 1


def agent_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = PRINT_AGENT_TOKEN
        got = request.headers.get('X-Print-Agent-Token', '')
        if not expected or not hmac.compare_digest(got, expected):
            return jsonify(error='Unauthorized print agent'), 401
        return fn(*args, **kwargs)
    return wrapper


@app.get('/')
def home():
    return render_template('index.html', key_id=RAZORPAY_KEY_ID, bw=PRICE_BW, color=PRICE_COLOR,
                           live_ready=bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET and PUBLIC_BASE_URL))


@app.get('/health')
def health():
    return jsonify(status='ok', app='Joswal Smart Print', payments_configured=bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET),
                   webhook_configured=bool(RAZORPAY_WEBHOOK_SECRET), public_url_configured=bool(PUBLIC_BASE_URL))


@app.post('/api/quote')
def quote():
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify(error='कृपया PDF या फोटो चुनें।'), 400
    ext = Path(secure_filename(f.filename)).suffix.lower()
    if ext not in ALLOWED:
        return jsonify(error='केवल PDF, JPG, JPEG या PNG फाइल स्वीकार है।'), 400
    mode = request.form.get('mode', 'bw')
    if mode not in {'bw', 'color'}:
        return jsonify(error='गलत प्रिंट मोड।'), 400
    try:
        copies = int(request.form.get('copies', '1'))
    except ValueError:
        return jsonify(error='कॉपी की संख्या सही नहीं है।'), 400
    if copies < 1 or copies > 20:
        return jsonify(error='कॉपी 1 से 20 के बीच रखें।'), 400
    # Save with a random name to avoid collisions/path traversal.
    order_id = uuid.uuid4().hex[:12].upper()
    stored_name = f'{order_id}{ext}'
    path = UPLOAD_DIR / stored_name
    f.save(path)
    if path.stat().st_size > MAX_BYTES:
        path.unlink(missing_ok=True)
        return jsonify(error='फाइल बहुत बड़ी है।'), 413
    pages = get_pdf_pages(path) if ext == '.pdf' else 1
    rate = PRICE_BW if mode == 'bw' else PRICE_COLOR
    amount_paise = pages * copies * rate * 100
    original_name = secure_filename(f.filename)[:180] or f'upload{ext}'
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        c.execute('INSERT INTO orders(id, original_name, stored_name, file_ext, pages, copies, color_mode, amount_paise, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (order_id, original_name, stored_name, ext, pages, copies, mode, amount_paise, 'uploaded', now))
    return jsonify(order_id=order_id, filename=original_name, pages=pages, copies=copies,
                   mode=mode, amount_rupees=amount_paise / 100,
                   note='अभी भुगतान नहीं हुआ है। भुगतान की पुष्टि के बाद ही प्रिंट कतार में जाएगा।')


@app.post('/api/create-payment')
def create_payment():
    if not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
        return jsonify(error='पेमेंट अभी सेट नहीं है। Razorpay Live/Test API keys को server environment में जोड़ना होगा।'), 503
    data = request.get_json(silent=True) or {}
    order_id = str(data.get('order_id', ''))
    with db() as c:
        row = c.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
    if not row:
        return jsonify(error='ऑर्डर नहीं मिला। दोबारा फाइल अपलोड करें।'), 404
    if row['status'] not in ('uploaded', 'payment_pending'):
        return jsonify(error='इस ऑर्डर के लिए पेमेंट शुरू नहीं हो सकती।'), 409
    try:
        import razorpay
        client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
        rp_order = client.order.create(data={'amount': row['amount_paise'], 'currency': 'INR', 'receipt': row['id'],
                                              'notes': {'local_order_id': row['id'], 'service': 'Joswal Smart Print'}})
    except Exception as e:
        app.logger.exception('Razorpay order creation failed')
        return jsonify(error='Razorpay order नहीं बन पाया। API key और dashboard status जाँचें।'), 502
    with db() as c:
        c.execute('UPDATE orders SET status=?, razorpay_order_id=? WHERE id=?', ('payment_pending', rp_order['id'], row['id']))
    return jsonify(key_id=RAZORPAY_KEY_ID, razorpay_order_id=rp_order['id'], amount=row['amount_paise'], currency='INR',
                   name='Sunil Kumar E Mitra', description='Joswal Smart Print order ' + row['id'], order_id=row['id'])


@app.post('/api/payment-failed')
def payment_failed():
    data = request.get_json(silent=True) or {}
    order_id = str(data.get('order_id', ''))
    with db() as c:
        c.execute("UPDATE orders SET status='payment_failed' WHERE id=? AND status='payment_pending'", (order_id,))
    return jsonify(ok=True)


@app.post('/razorpay/webhook')
def razorpay_webhook():
    if not RAZORPAY_WEBHOOK_SECRET:
        return 'Webhook secret not configured', 503
    body = request.get_data()
    signature = request.headers.get('X-Razorpay-Signature', '')
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return 'Invalid signature', 400
    try:
        event = json.loads(body.decode('utf-8'))
    except Exception:
        return 'Invalid JSON', 400
    if event.get('event') == 'payment.captured':
        payment = event.get('payload', {}).get('payment', {}).get('entity', {})
        rp_order_id = payment.get('order_id')
        payment_id = payment.get('id')
        with db() as c:
            row = c.execute('SELECT id FROM orders WHERE razorpay_order_id=?', (rp_order_id,)).fetchone()
            if row:
                # Only server-verified captured payment can trigger the print queue.
                c.execute("UPDATE orders SET status='paid', razorpay_payment_id=? WHERE id=? AND status IN ('payment_pending','uploaded')", (payment_id, row['id']))
    elif event.get('event') == 'payment.failed':
        payment = event.get('payload', {}).get('payment', {}).get('entity', {})
        rp_order_id = payment.get('order_id')
        with db() as c:
            c.execute("UPDATE orders SET status='payment_failed' WHERE razorpay_order_id=? AND status='payment_pending'", (rp_order_id,))
    return 'ok', 200


@app.get('/api/print-jobs')
@agent_required
def print_jobs():
    with db() as c:
        rows = c.execute("SELECT id, original_name, stored_name, file_ext, pages, copies, color_mode FROM orders WHERE status='paid' ORDER BY created_at LIMIT 5").fetchall()
    jobs = []
    for r in rows:
        jobs.append({'order_id': r['id'], 'filename': r['original_name'], 'file_ext': r['file_ext'], 'pages': r['pages'],
                     'copies': r['copies'], 'mode': r['color_mode'], 'download_url': f"{PUBLIC_BASE_URL}/api/print-jobs/{r['id']}/file" if PUBLIC_BASE_URL else f"/api/print-jobs/{r['id']}/file"})
    return jsonify(jobs=jobs)


@app.get('/api/print-jobs/<order_id>/file')
@agent_required
def download_print_file(order_id):
    with db() as c:
        row = c.execute("SELECT stored_name, original_name, status FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row or row['status'] != 'paid':
        abort(404)
    path = UPLOAD_DIR / row['stored_name']
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=row['original_name'])


@app.post('/api/print-jobs/<order_id>/complete')
@agent_required
def complete_print_job(order_id):
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in {'printed', 'error'}:
        return jsonify(error='status must be printed or error'), 400
    with db() as c:
        if status == 'printed':
            c.execute("UPDATE orders SET status='printed', printed_at=? WHERE id=? AND status='paid'", (datetime.now(timezone.utc).isoformat(), order_id))
        else:
            msg = str(data.get('message', 'Print agent error'))[:500]
            c.execute("UPDATE orders SET status='print_error', error=? WHERE id=? AND status='paid'", (msg, order_id))
    return jsonify(ok=True)


@app.get('/admin/orders')
def admin_orders():
    token = os.getenv('ADMIN_TOKEN', '')
    if not token or request.args.get('token', '') != token:
        abort(404)
    with db() as c:
        rows = c.execute('SELECT id, original_name, pages, copies, color_mode, amount_paise, status, created_at, printed_at, error FROM orders ORDER BY created_at DESC LIMIT 100').fetchall()
    return render_template('orders.html', rows=rows)


@app.errorhandler(413)
def too_large(_):
    return jsonify(error=f'फाइल {MAX_BYTES // (1024*1024)} MB से बड़ी है।'), 413

init_db()
if __name__ == '__main__':
    # Local development only. For a public site use HTTPS and a production WSGI server.
    app.run(host=os.getenv('HOST', '127.0.0.1'), port=int(os.getenv('PORT', '5000')), debug=False, threaded=True)
