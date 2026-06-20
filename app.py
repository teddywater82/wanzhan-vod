#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频点播网站 - 后端主程序
功能：视频流媒体播放、15秒试看、会员系统、四方支付对接
"""

import os
import time
import json
import uuid
import hmac
import hashlib
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlencode

from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, send_from_directory, abort, Response,
                   make_response)
from werkzeug.security import generate_password_hash, check_password_hash

# ============================================================
# 配置
# ============================================================
app = Flask(__name__)
app.secret_key = os.urandom(24).hex()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data', 'site.db')
VIDEO_DIR = os.path.join(BASE_DIR, 'videos')
QR_DIR = os.path.join(BASE_DIR, 'static', 'qrcodes')
MEMBERSHIP_PRICE = 29.9  # 会员价格（元）
TRIAL_SECONDS = 15       # 试看秒数

os.makedirs(os.path.join(BASE_DIR, 'data'), exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, 'static', 'qrcodes'), exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, 'static', 'uploads'), exist_ok=True)


# ============================================================
# 数据库初始化
# ============================================================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            is_vip INTEGER DEFAULT 0,
            vip_expire TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            filename TEXT NOT NULL,
            cover TEXT DEFAULT '',
            duration INTEGER DEFAULT 0,
            category TEXT DEFAULT '默认',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT UNIQUE NOT NULL,
            user_id INTEGER DEFAULT 0,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            pay_type TEXT DEFAULT '',
            trade_no TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            paid_at TEXT
        );

        CREATE TABLE IF NOT EXISTS pay_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            config TEXT NOT NULL,
            enabled INTEGER DEFAULT 1
        );
    """)
    conn.commit()
    conn.close()


# ============================================================
# 四方支付对接（聚合支付）
# ============================================================
class PayService:
    """四方支付/聚合支付接入服务
    支持自定义配置，可对接任意支持回调的四方支付平台
    """

    @staticmethod
    def get_config():
        """获取支付配置"""
        conn = get_db()
        row = conn.execute("SELECT config FROM pay_config WHERE name='fourth_pay' AND enabled=1").fetchone()
        conn.close()
        if row:
            return json.loads(row['config'])
        # 默认配置（用户需自行替换）
        return {
            "api_url": "https://api.example.com/gateway/pay",  # 四方支付网关地址
            "app_id": "your_app_id",
            "app_secret": "your_app_secret",
            "notify_url": "https://your-domain.com/pay/notify",
            "return_url": "https://your-domain.com/pay/success"
        }

    @staticmethod
    def create_order(amount, order_no, pay_type='alipay'):
        """创建支付订单"""
        config = PayService.get_config()

        params = {
            "app_id": config['app_id'],
            "order_no": order_no,
            "amount": f"{amount:.2f}",
            "pay_type": pay_type,  # alipay / wxpay / qqpay
            "notify_url": config['notify_url'],
            "return_url": config['return_url'],
            "timestamp": str(int(time.time()))
        }

        # 排序并生成签名
        sorted_params = dict(sorted(params.items()))
        sign_str = '&'.join([f"{k}={v}" for k, v in sorted_params.items()])
        sign_str += f"&key={config['app_secret']}"
        params['sign'] = hashlib.md5(sign_str.encode()).hexdigest().upper()

        return config['api_url'], params

    @staticmethod
    def verify_notify(data):
        """验证支付回调签名"""
        config = PayService.get_config()
        if not data:
            return False

        sign = data.pop('sign', '')
        sorted_params = dict(sorted(data.items()))
        sign_str = '&'.join([f"{k}={v}" for k, v in sorted_params.items()])
        sign_str += f"&key={config['app_secret']}"
        calc_sign = hashlib.md5(sign_str.encode()).hexdigest().upper()
        return calc_sign == sign


# ============================================================
# 视频服务 - 支持15秒试看
# ============================================================
class VideoService:
    """视频文件服务，支持断点续传和15秒试看限制"""

    @staticmethod
    def get_video_path(filename):
        """获取视频文件路径"""
        path = os.path.join(VIDEO_DIR, filename)
        if os.path.exists(path):
            return path
        return None

    @staticmethod
    def stream_video(filepath, range_header=None, max_seconds=None):
        """流式传输视频，支持HTTP Range和试看限制"""
        file_size = os.path.getsize(filepath)
        content_type = 'video/mp4'

        # 未登录且非会员时限制时长
        if max_seconds and max_seconds > 0:
            max_bytes = VideoService._get_bytes_for_duration(filepath, max_seconds)
            if max_bytes and max_bytes < file_size:
                file_size = max_bytes

        if range_header:
            start, end = 0, file_size - 1
            range_str = range_header.replace('bytes=', '')
            parts = range_str.split('-')
            if parts[0]:
                start = int(parts[0])
            if parts[1]:
                end = int(parts[1])

            length = end - start + 1
            if start >= file_size:
                return Response(status=416)

            def generate():
                with open(filepath, 'rb') as f:
                    f.seek(start)
                    remaining = length
                    chunk_size = 8192
                    while remaining > 0:
                        chunk = f.read(min(chunk_size, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk

            resp = Response(generate(), status=206,
                            mimetype=content_type)
            resp.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
            resp.headers['Content-Length'] = str(length)
            return resp
        else:
            def generate():
                with open(filepath, 'rb') as f:
                    remaining = file_size
                    chunk_size = 8192
                    while remaining > 0:
                        chunk = f.read(min(chunk_size, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk

            resp = Response(generate(), mimetype=content_type)
            resp.headers['Content-Length'] = str(file_size)
            return resp

    @staticmethod
    def _get_bytes_for_duration(filepath, seconds):
        """根据时长估算对应字节数（近似）"""
        try:
            import subprocess
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries',
                 'format=duration', '-of',
                 'default=noprint_wrappers=1:nokey=1', filepath],
                capture_output=True, text=True, timeout=10
            )
            duration = float(result.stdout.strip())
            file_size = os.path.getsize(filepath)
            if duration > 0:
                bytes_per_sec = file_size / duration
                return int(bytes_per_sec * seconds)
        except Exception:
            pass
        # 如果ffprobe不可用，按总大小20%估算
        return int(os.path.getsize(filepath) * 0.2)


# ============================================================
# 辅助函数
# ============================================================
def generate_order_no():
    """生成唯一订单号"""
    return f"MG{datetime.now().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:8]}"


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ============================================================
# 路由 - 页面
# ============================================================
@app.route('/')
def index():
    """首页 - 视频列表"""
    conn = get_db()
    videos = conn.execute(
        "SELECT * FROM videos ORDER BY sort_order ASC, created_at DESC"
    ).fetchall()
    conn.close()

    user = None
    if 'user_id' in session:
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE id=?", (session['user_id'],)).fetchone()
        conn.close()

    return render_template('index.html', videos=videos, user=user,
                           trial=TRIAL_SECONDS, price=MEMBERSHIP_PRICE)


@app.route('/video/<int:video_id>')
def video_detail(video_id):
    """视频详情页"""
    conn = get_db()
    video = conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
    user = None
    is_vip = False

    if 'user_id' in session:
        user = conn.execute("SELECT * FROM users WHERE id=?", (session['user_id'],)).fetchone()
        if user and user['is_vip']:
            expire = user['vip_expire']
            if expire and datetime.strptime(expire, '%Y-%m-%d %H:%M:%S') > datetime.now():
                is_vip = True

    conn.close()

    if not video:
        abort(404)

    return render_template('video.html', video=video, user=user,
                           is_vip=is_vip, trial=TRIAL_SECONDS)


@app.route('/login', methods=['GET', 'POST'])
def login():
    """登录"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()

        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('index'))
        return render_template('login.html', error='用户名或密码错误')

    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    """注册"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        if len(username) < 3 or len(password) < 6:
            return render_template('register.html', error='用户名至少3位，密码至少6位')

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO users (username, password) VALUES (?, ?)",
                (username, generate_password_hash(password))
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return render_template('register.html', error='用户名已存在')
        conn.close()

        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/logout')
def logout():
    """退出"""
    session.clear()
    return redirect(url_for('index'))


@app.route('/member')
@login_required
def member_center():
    """会员中心"""
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session['user_id'],)).fetchone()

    # 查询历史订单
    orders = conn.execute(
        "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC",
        (session['user_id'],)
    ).fetchall()
    conn.close()

    return render_template('member.html', user=user, orders=orders,
                           price=MEMBERSHIP_PRICE)


# ============================================================
# 路由 - 视频流（支持15秒试看限制）
# ============================================================
@app.route('/api/video/stream/<int:video_id>')
def video_stream(video_id):
    """视频流接口 - 支持15秒试看跳转"""
    conn = get_db()
    video = conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
    if not video:
        conn.close()
        abort(404)

    filepath = VideoService.get_video_path(video['filename'])
    if not filepath:
        conn.close()
        abort(404)

    # 检查是否VIP
    is_vip = False
    user_id = session.get('user_id')
    if user_id:
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if user and user['is_vip']:
            expire = user['vip_expire']
            if expire and datetime.strptime(expire, '%Y-%m-%d %H:%M:%S') > datetime.now():
                is_vip = True
    conn.close()

    # 非VIP限制试看时长
    max_seconds = None if is_vip else TRIAL_SECONDS
    range_header = request.headers.get('Range')

    return VideoService.stream_video(filepath, range_header, max_seconds)


# ============================================================
# 路由 - 支付（四方支付）
# ============================================================
@app.route('/api/pay/create', methods=['POST'])
@login_required
def pay_create():
    """创建支付订单"""
    data = request.get_json()
    pay_type = data.get('pay_type', 'alipay')  # alipay, wxpay, qqpay

    order_no = generate_order_no()
    amount = MEMBERSHIP_PRICE

    conn = get_db()
    conn.execute(
        "INSERT INTO orders (order_no, user_id, amount, pay_type) VALUES (?, ?, ?, ?)",
        (order_no, session['user_id'], amount, pay_type)
    )
    conn.commit()
    conn.close()

    # 调用四方支付
    pay_url, pay_params = PayService.create_order(amount, order_no, pay_type)

    return jsonify({
        'code': 0,
        'data': {
            'order_no': order_no,
            'amount': amount,
            'pay_url': pay_url,
            'pay_params': pay_params
        }
    })


@app.route('/api/pay/check/<order_no>')
@login_required
def pay_check(order_no):
    """查询订单状态"""
    conn = get_db()
    order = conn.execute(
        "SELECT * FROM orders WHERE order_no=? AND user_id=?",
        (order_no, session['user_id'])
    ).fetchone()
    conn.close()

    if not order:
        return jsonify({'code': 1, 'msg': '订单不存在'})

    return jsonify({
        'code': 0,
        'data': {
            'status': order['status'],
            'order_no': order['order_no']
        }
    })


@app.route('/pay/notify', methods=['POST'])
def pay_notify():
    """四方支付异步回调"""
    data = request.form.to_dict()
    if not data:
        data = request.get_json(silent=True) or {}

    # 验证签名
    if not PayService.verify_notify(data):
        return 'sign_error'

    order_no = data.get('order_no', '')
    trade_no = data.get('trade_no', '')
    status = data.get('status', '')

    if status == 'success' or data.get('pay_status') == '1':
        conn = get_db()
        order = conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
        if order and order['status'] == 'pending':
            # 更新订单
            conn.execute(
                "UPDATE orders SET status='paid', trade_no=?, paid_at=datetime('now','localtime') WHERE order_no=?",
                (trade_no, order_no)
            )
            # 开通会员（1年有效期）
            expire_time = datetime.now() + timedelta(days=365)
            conn.execute(
                "UPDATE users SET is_vip=1, vip_expire=? WHERE id=?",
                (expire_time.strftime('%Y-%m-%d %H:%M:%S'), order['user_id'])
            )
            conn.commit()
        conn.close()
        return 'success'

    return 'fail'


@app.route('/pay/success')
def pay_success():
    """支付成功页"""
    return render_template('pay_success.html')


# ============================================================
# 路由 - 管理后台
# ============================================================
@app.route('/admin')
def admin_login_page():
    """管理员登录页"""
    return render_template('admin_login.html')


@app.route('/admin/login', methods=['POST'])
def admin_login():
    """管理员登录"""
    username = request.form.get('username', '')
    password = request.form.get('password', '')

    if username == 'admin' and password == 'admin888':
        session['is_admin'] = True
        return redirect(url_for('admin_dashboard'))
    return render_template('admin_login.html', error='账号或密码错误')


@app.route('/admin/dashboard')
def admin_dashboard():
    """管理后台首页"""
    if not session.get('is_admin'):
        return redirect(url_for('admin_login_page'))

    conn = get_db()
    video_count = conn.execute("SELECT COUNT(*) as c FROM videos").fetchone()['c']
    user_count = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()['c']
    vip_count = conn.execute("SELECT COUNT(*) as c FROM users WHERE is_vip=1").fetchone()['c']
    order_count = conn.execute("SELECT COUNT(*) as c FROM orders WHERE status='paid'").fetchone()['c']
    total_revenue = conn.execute("SELECT COALESCE(SUM(amount),0) as s FROM orders WHERE status='paid'").fetchone()['s']
    conn.close()

    return render_template('admin_dashboard.html',
                           video_count=video_count,
                           user_count=user_count,
                           vip_count=vip_count,
                           order_count=order_count,
                           total_revenue=total_revenue)


@app.route('/admin/videos')
def admin_videos():
    """视频管理"""
    if not session.get('is_admin'):
        return redirect(url_for('admin_login_page'))

    conn = get_db()
    videos = conn.execute("SELECT * FROM videos ORDER BY sort_order ASC, created_at DESC").fetchall()
    conn.close()
    return render_template('admin_videos.html', videos=videos)


@app.route('/admin/video/add', methods=['POST'])
def admin_video_add():
    """添加视频"""
    if not session.get('is_admin'):
        return jsonify({'code': 1, 'msg': '未登录'})

    title = request.form.get('title', '').strip()
    desc = request.form.get('description', '')
    category = request.form.get('category', '默认')
    sort_order = int(request.form.get('sort_order', 0))

    # 处理视频上传
    video_file = request.files.get('video_file')
    cover_file = request.files.get('cover_file')
    video_url = request.form.get('video_url', '').strip()

    filename = None
    if video_file and video_file.filename:
        ext = os.path.splitext(video_file.filename)[1]
        filename = f"{uuid.uuid4().hex}{ext}"
        video_file.save(os.path.join(VIDEO_DIR, filename))
    elif video_url:
        filename = video_url  # 外部链接

    cover = ''
    if cover_file and cover_file.filename:
        ext = os.path.splitext(cover_file.filename)[1]
        cover = f"{uuid.uuid4().hex}{ext}"
        cover_file.save(os.path.join(app.static_folder or 'static', 'uploads', cover))

    if not filename:
        return jsonify({'code': 1, 'msg': '请上传视频或填写视频链接'})

    conn = get_db()
    conn.execute(
        "INSERT INTO videos (title, description, filename, cover, category, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
        (title, desc, filename, cover, category, sort_order)
    )
    conn.commit()
    conn.close()

    return redirect(url_for('admin_videos'))


@app.route('/admin/video/delete/<int:video_id>')
def admin_video_delete(video_id):
    """删除视频"""
    if not session.get('is_admin'):
        return redirect(url_for('admin_login_page'))

    conn = get_db()
    video = conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
    if video:
        # 删除本地文件
        local_path = os.path.join(VIDEO_DIR, video['filename'])
        if os.path.exists(local_path):
            os.remove(local_path)
        conn.execute("DELETE FROM videos WHERE id=?", (video_id,))
        conn.commit()
    conn.close()

    return redirect(url_for('admin_videos'))


@app.route('/admin/orders')
def admin_orders():
    """订单管理"""
    if not session.get('is_admin'):
        return redirect(url_for('admin_login_page'))

    conn = get_db()
    orders = conn.execute("""
        SELECT o.*, u.username FROM orders o
        LEFT JOIN users u ON o.user_id = u.id
        ORDER BY o.created_at DESC
    """).fetchall()
    conn.close()
    return render_template('admin_orders.html', orders=orders)


@app.route('/admin/pay_config', methods=['GET', 'POST'])
def admin_pay_config():
    """支付配置"""
    if not session.get('is_admin'):
        return redirect(url_for('admin_login_page'))

    if request.method == 'POST':
        config = {
            "api_url": request.form.get('api_url', ''),
            "app_id": request.form.get('app_id', ''),
            "app_secret": request.form.get('app_secret', ''),
            "notify_url": request.form.get('notify_url', ''),
            "return_url": request.form.get('return_url', '')
        }
        conn = get_db()
        existing = conn.execute("SELECT id FROM pay_config WHERE name='fourth_pay'").fetchone()
        if existing:
            conn.execute("UPDATE pay_config SET config=?, enabled=1 WHERE name='fourth_pay'", (json.dumps(config),))
        else:
            conn.execute("INSERT INTO pay_config (name, config, enabled) VALUES ('fourth_pay', ?, 1)", (json.dumps(config),))
        conn.commit()
        conn.close()
        return redirect(url_for('admin_pay_config'))

    conn = get_db()
    row = conn.execute("SELECT config FROM pay_config WHERE name='fourth_pay'").fetchone()
    conn.close()
    pay_config = json.loads(row['config']) if row else {
        "api_url": "",
        "app_id": "",
        "app_secret": "",
        "notify_url": "",
        "return_url": ""
    }

    return render_template('admin_pay.html', config=pay_config)


@app.route('/admin/logout')
def admin_logout():
    """管理员退出"""
    session.pop('is_admin', None)
    return redirect(url_for('admin_login_page'))


# ============================================================
# 启动
# ============================================================
if __name__ == '__main__':
    init_db()
    print(f"[小白龙] 视频点播网站已启动")
    print(f"[小白龙] 管理后台: http://127.0.0.1:5000/admin")
    print(f"[小白龙] 管理员账号: admin / admin888")
    app.run(host='0.0.0.0', port=5000, debug=True)
