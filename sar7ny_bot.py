#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
صارحني PRO — بوت رسائل مجهولة على تيليغرام.

• كود دائم ورابط خاص لكل مستخدم: https://t.me/<اسم_البوت>?start=<الكود>
• الرسالة المجهولة تصل المستلم بلا هوية المرسل.
• زر «↩️ رد مجهول» أو السحب للرد: الرد يصل المرسل الأصلي دون كشف المستلم.
• النشر التلقائي: كل رسالة جديدة تُنشر في القناة مع زر «أرسل رسالة مجهولة».
• المالك وحده يرى هوية الطرفين في كل إشعار، ويدير الحظر والقناة والإيقاف.
• مكتبات بايثون القياسية فقط (لا يحتاج pip)، وقاعدة بيانات SQLite.

التشغيل:
    export BOT_TOKEN="توكن_البوت"
    export OWNER_ID="8622450463"
    python3 sar7ny_bot.py
"""

import html
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone

# ----------------------------------------------------------------- الإعدادات
# متغير البيئة BOT_TOKEN له الأولوية، وإن لم يوجد يُستخدم التوكن الافتراضي أدناه.
DEFAULT_BOT_TOKEN = "9170405406:AAEVFuZu6Q5LkGIHQp4C9lkfh2m5qaQyIo8"
BOT_TOKEN = os.environ.get("BOT_TOKEN", DEFAULT_BOT_TOKEN).strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "8622450463"))
# قاعدة البيانات دائماً بجوار هذا الملف، بغضّ النظر عن مجلد التشغيل الحالي،
# حتى لا تُفتح قاعدة فارغة جديدة (وتضيع الأكواد السابقة) إذا تغيّر مجلد العمل.
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sar7ny.db"))

MAX_TEXT_CHARS = 1000
CODE_LENGTH = 8
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # بلا I و O والصفر والواحد
POLL_TIMEOUT = 30

API_URL = "https://api.telegram.org/bot{token}/{method}"
BOT_USERNAME = ""  # يُملأ من getMe عند التشغيل

# ----------------------------------------------------------------- الشكل
# كل ألوان الرسائل ورموزها من هنا. غيّرها كما تشاء دون لمس بقية الكود.
DIV = "━━━━━━━━━━━━━━"
THEME = {
    "incoming": "🟣",  # رسالة مجهولة تصل المستلم
    "reply_in": "🟢",  # رد يصل المرسل الأصلي
    "owner":    "🔵",  # إشعارات المالك
    "ok":       "✅",
    "warn":     "⚠️",
    "ban":      "🚫",
    "pause":    "⏸",
    "channel":  "📢",
    "write":    "✍️",
    "key":      "🔑",
    "link":     "🔗",
    "hidden":   "🤫",
}

# الألوان المتاحة لرسائل «واردة» عبر أمر المالك /setcolor <اللون>
COLOR_PALETTE = {
    "بنفسجي": "🟣", "ارجواني": "🟣", "أرجواني": "🟣",
    "اخضر": "🟢", "أخضر": "🟢",
    "ازرق": "🔵", "أزرق": "🔵",
    "احمر": "🔴", "أحمر": "🔴",
    "برتقالي": "🟠",
    "اصفر": "🟡", "أصفر": "🟡",
    "اسود": "⚫", "أسود": "⚫",
    "ابيض": "⚪", "أبيض": "⚪",
    "بني": "🟤",
}

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("sar7ny")


# ------------------------------------------------------------ قاعدة البيانات
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                code       TEXT UNIQUE NOT NULL,
                first_name TEXT,
                username   TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id     INTEGER NOT NULL,
                recipient_id  INTEGER NOT NULL,
                sender_msg_id INTEGER NOT NULL,
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                recipient_id     INTEGER NOT NULL,
                recipient_msg_id INTEGER NOT NULL,
                message_id       INTEGER NOT NULL,
                PRIMARY KEY (recipient_id, recipient_msg_id)
            );
            CREATE TABLE IF NOT EXISTS reply_mode (
                user_id    INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id   INTEGER PRIMARY KEY,
                banned_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending (
                user_id    INTEGER PRIMARY KEY,
                target_id  INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)


def _new_code(conn):
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if conn.execute("SELECT 1 FROM users WHERE code = ?", (code,)).fetchone() is None:
            return code


def register_user(tg_user):
    """ينشئ المستخدم إن لم يكن موجوداً. الكود يُولَّد مرة واحدة ولا يتغير أبداً."""
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, code, first_name, username, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (tg_user["id"], _new_code(conn), tg_user.get("first_name", ""),
             tg_user.get("username"), now()),
        )
        conn.execute("UPDATE users SET first_name = ?, username = ? WHERE user_id = ?",
                     (tg_user.get("first_name", ""), tg_user.get("username"), tg_user["id"]))
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (tg_user["id"],)).fetchone()


def find_user_by_id(user_id):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def find_user_by_code(code):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE code = ?", (code,)).fetchone()


def is_banned(user_id):
    with db() as conn:
        return conn.execute("SELECT 1 FROM banned_users WHERE user_id = ?",
                            (user_id,)).fetchone() is not None


def set_ban(user_id, banned):
    with db() as conn:
        if banned:
            conn.execute("INSERT OR REPLACE INTO banned_users (user_id, banned_at) VALUES (?, ?)",
                         (user_id, now()))
        else:
            conn.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))


def set_pending(user_id, target_id):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO pending (user_id, target_id, updated_at) VALUES (?, ?, ?)",
                     (user_id, target_id, now()))


def get_pending(user_id):
    with db() as conn:
        row = conn.execute("SELECT target_id FROM pending WHERE user_id = ?", (user_id,)).fetchone()
        return row["target_id"] if row else None


def clear_pending(user_id):
    with db() as conn:
        conn.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))


def set_reply_mode(user_id, message_id):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO reply_mode (user_id, message_id, updated_at) VALUES (?, ?, ?)",
                     (user_id, message_id, now()))


def get_reply_mode(user_id):
    with db() as conn:
        row = conn.execute("SELECT message_id FROM reply_mode WHERE user_id = ?", (user_id,)).fetchone()
        return row["message_id"] if row else None


def clear_reply_mode(user_id):
    with db() as conn:
        conn.execute("DELETE FROM reply_mode WHERE user_id = ?", (user_id,))


def save_message(sender_id, recipient_id, sender_msg_id):
    """يحفظ الرسالة المجهولة ويعيد معرّفها الداخلي (يُستخدم في أزرار الرد)."""
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO messages (sender_id, recipient_id, sender_msg_id, created_at) VALUES (?, ?, ?, ?)",
            (sender_id, recipient_id, sender_msg_id, now()),
        )
        return cur.lastrowid


def delete_message(message_id):
    with db() as conn:
        conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))


def save_delivery(recipient_id, recipient_msg_id, message_id):
    """يربط رسالة أُرسلت للمستلم (معرّفها في تيليغرام) بالرسالة الداخلية، ليعمل السحب للرد."""
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO deliveries (recipient_id, recipient_msg_id, message_id) "
                     "VALUES (?, ?, ?)", (recipient_id, recipient_msg_id, message_id))


def find_delivery_message_id(recipient_id, recipient_msg_id):
    with db() as conn:
        row = conn.execute("SELECT message_id FROM deliveries WHERE recipient_id = ? AND recipient_msg_id = ?",
                           (recipient_id, recipient_msg_id)).fetchone()
        return row["message_id"] if row else None


def find_message_for_recipient(recipient_id, message_id):
    """يعيد الرسالة فقط إذا كان المستخدم هو مستلمها، وإلا None."""
    with db() as conn:
        return conn.execute("SELECT * FROM messages WHERE id = ? AND recipient_id = ?",
                            (message_id, recipient_id)).fetchone()


def get_setting(key, default):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def is_paused():
    return get_setting("paused", "0") == "1"


def autopublish_on():
    return get_setting("autopublish", "0") == "1"


def get_incoming_icon():
    return get_setting("incoming_color", THEME["incoming"])


def get_stats():
    with db() as conn:
        return {
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "banned": conn.execute("SELECT COUNT(*) FROM banned_users").fetchone()[0],
        }


def get_users_page(page, per_page=15):
    """يعيد (rows, total) لصفحة من جدول المستخدمين، الأحدث أولاً."""
    page = max(page, 1)
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (per_page, (page - 1) * per_page),
        ).fetchall()
    return rows, total


def clean_code(raw):
    """ينظّف الكود من أي رموز غير مرئية (مثل علامات اتجاه الكتابة في لوحات المفاتيح
    العربية) ومن المسافات، ويحوّله لحروف كبيرة، ليطابق الكود المخزَّن دوماً."""
    return re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()


# ----------------------------------------------------------- واجهة تيليغرام
def tg(method, http_timeout=40, **params):
    """يستدعي Telegram Bot API ويعيد الحقل result، أو None عند الفشل."""
    url = API_URL.format(token=BOT_TOKEN, method=method)
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode("utf-8")
    try:
        with urllib.request.urlopen(url, data=data, timeout=http_timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        try:
            payload = json.loads(err.read().decode("utf-8"))
        except (ValueError, OSError):
            log.error("%s: خطأ HTTP %s", method, err.code)
            return None
    except (OSError, ValueError) as err:
        log.error("%s: %s", method, type(err).__name__)
        return None

    if not payload.get("ok"):
        log.warning("%s: %s", method, payload.get("description"))
        return None
    return payload.get("result")


def _json(obj):
    return json.dumps(obj, ensure_ascii=False) if obj is not None else None


def say(chat_id, text, reply_markup=None, reply_to=None):
    """يرسل رسالة HTML منسّقة. أي نص ديناميكي يجب أن يمر عبر esc()."""
    return tg(
        "sendMessage",
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview="true",
        reply_markup=_json(reply_markup),
        reply_to_message_id=reply_to,
        allow_sending_without_reply="true" if reply_to else None,
    )


def copy_message(chat_id, from_chat_id, message_id, reply_to=None, reply_markup=None):
    """ينسخ الوسائط (صورة، صوت، ملصق...) دون ذكر المرسل الأصلي."""
    return tg(
        "copyMessage",
        chat_id=chat_id,
        from_chat_id=from_chat_id,
        message_id=message_id,
        reply_to_message_id=reply_to,
        allow_sending_without_reply="true" if reply_to else None,
        reply_markup=_json(reply_markup),
    )


def answer_callback(cq_id, text=None):
    tg("answerCallbackQuery", callback_query_id=cq_id, text=text)


# ------------------------------------------------------------ بناء الرسائل
def esc(value):
    """يهرب النص ليكون آمناً داخل HTML تيليغرام."""
    return html.escape(str(value), quote=False)


def card(icon, title, lines=(), footer=None):
    out = [f"{icon} <b>{title}</b>", DIV]
    out.extend(lines)
    if footer:
        out += [DIV, footer]
    return "\n".join(out)


def who(name, username, uid):
    handle = f"@{esc(username)}" if username else "بدون يوزر"
    return f"{esc(name or 'بدون اسم')} · {handle} · ID <code>{uid}</code>"


def link_for(code):
    return f"https://t.me/{BOT_USERNAME}?start={code}"


def reply_button(message_id):
    return {"inline_keyboard": [[{"text": "↩️ رد مجهول", "callback_data": f"r:{message_id}"}]]}


def link_button(code):
    return {"inline_keyboard": [[{"text": "✉️ أرسل رسالة مجهولة", "url": link_for(code)}]]}


def paused_card():
    return card(THEME["pause"], "الاستقبال متوقف مؤقتاً", ["حاول لاحقاً."])


def too_long_card():
    return card(THEME["warn"], "الرسالة طويلة", [f"الحد الأقصى {MAX_TEXT_CHARS} حرف."])


def incoming_card(body):
    return card(get_incoming_icon(), "رسالة مجهولة وصلتك", [body],
                footer=f"{THEME['hidden']} <i>المرسل مجهول تماماً · اضغط «رد مجهول» للرد</i>")


def reply_card(body):
    return card(THEME["reply_in"], "وصلك رد على رسالتك", [body],
                footer=f"{THEME['hidden']} <i>رد مجهول من صاحب الرابط</i>")


def channel_text_card(message_id, text):
    return card(THEME["hidden"], f"رسالة مجهولة #{message_id}", [esc(text)],
                footer="✉️ أرسل رسالتك المجهولة من الزر بالأسفل")


# ----------------------------------------------------------- منطق المستخدم
def send_welcome(user):
    link = link_for(user["code"])
    lines = [
        f"{THEME['key']} كودك الخاص: <code>{esc(user['code'])}</code>",
        f"{THEME['link']} رابطك:",
        f"<code>{esc(link)}</code>",
    ]
    footer = "شارك الرابط مع أصدقائك، وستصلك رسائلهم هنا بشكل مجهول تماماً."
    if user["user_id"] == OWNER_ID:
        footer += "\n🛠 أوامر المالك: /help"
    say(user["user_id"], card(THEME["hidden"], "أهلاً بك في صارحني", lines, footer=footer))


def open_link(sender, code):
    uid = sender["user_id"]
    target = find_user_by_code(code)
    if target is None:
        return say(uid, card(THEME["warn"], "رابط غير صالح", ["تأكد من نسخ الرابط كاملاً."]))
    if target["user_id"] == uid:
        return say(uid, card(THEME["warn"], "لا يمكنك مراسلة نفسك", ["شارك رابطك مع غيرك ليراسلك."]))
    if uid != OWNER_ID and is_paused():
        return say(uid, paused_card())
    set_pending(uid, target["user_id"])
    clear_reply_mode(uid)
    say(uid, card(THEME["write"], "اكتب رسالتك الآن",
                  ["ستصل للمستلم مجهولة تماماً.", "نص · صورة · فيديو · صوت · ملصق"],
                  footer="لإلغاء الإرسال: /cancel"))


def _send_to_recipient(recipient_id, sender_id, msg, msg_db_id):
    """يرسل الرسالة للمستلم مجهولة. يعيد قائمة معرّفات ما أُرسل، أو None عند الفشل."""
    if msg.get("text") is not None:
        sent = say(recipient_id, incoming_card(esc(msg["text"])), reply_markup=reply_button(msg_db_id))
        return [sent["message_id"]] if sent else None
    header = say(recipient_id, incoming_card("⬇️ المحتوى بالأسفل"), reply_markup=reply_button(msg_db_id))
    if header is None:
        return None
    copied = copy_message(recipient_id, sender_id, msg["message_id"])
    if copied is None:
        return None
    return [header["message_id"], copied["message_id"]]


def publish_to_channel(msg, sender_id, msg_db_id, recipient):
    """ينشر الرسالة المجهولة في القناة مع زر يفتح رابط المستلم. يعيد النتيجة أو None."""
    channel = get_setting("channel", "")
    markup = link_button(recipient["code"])
    if msg.get("text") is not None:
        return say(channel, channel_text_card(msg_db_id, msg["text"]), reply_markup=markup)
    return copy_message(channel, sender_id, msg["message_id"], reply_markup=markup)


def notify_owner(title, lines, msg, from_chat_id):
    """إشعار للمالك بهوية الطرفين. النص يُدرج داخل الإشعار، والوسائط تُنسخ بعده."""
    if msg.get("text") is not None:
        say(OWNER_ID, card(THEME["owner"], title, list(lines) + [DIV, f"💬 {esc(msg['text'])}"]))
    else:
        say(OWNER_ID, card(THEME["owner"], title, lines))
        copy_message(OWNER_ID, from_chat_id, msg["message_id"])


def deliver(msg, tg_user):
    uid = tg_user["id"]
    target_id = get_pending(uid)
    if target_id is None:
        return say(uid, card(THEME["write"], "افتح رابط أولاً",
                             ["لإرسال رسالة مجهولة افتح رابط الشخص الذي تريد مراسلته."]))
    if uid != OWNER_ID and is_paused():
        return say(uid, paused_card())
    text = msg.get("text")
    if text is not None and len(text) > MAX_TEXT_CHARS:
        return say(uid, too_long_card())
    recipient = find_user_by_id(target_id)
    if recipient is None:
        clear_pending(uid)
        return say(uid, card(THEME["warn"], "المستلم غير متاح", ["افتح الرابط من جديد."]))

    msg_db_id = save_message(uid, target_id, msg["message_id"])
    ids = _send_to_recipient(target_id, uid, msg, msg_db_id)
    if ids is None:
        delete_message(msg_db_id)
        return say(uid, card(THEME["warn"], "تعذّر إيصال رسالتك", ["قد يكون المستلم أوقف البوت. حاول لاحقاً."]))
    for mid in ids:
        save_delivery(target_id, mid, msg_db_id)

    published = None
    if autopublish_on():
        if get_setting("channel", ""):
            published = publish_to_channel(msg, uid, msg_db_id, recipient) is not None
        else:
            published = False

    if uid != OWNER_ID:
        lines = [
            f"👤 المرسل: {who(tg_user.get('first_name'), tg_user.get('username'), uid)}",
            f"🎯 المستلم: {who(recipient['first_name'], recipient['username'], target_id)}"
            f" · كود <code>{esc(recipient['code'])}</code>",
        ]
        if published is not None:
            lines.append(f"{THEME['channel']} النشر في القناة: {'تم ✅' if published else 'لم يُنشر ⚠️'}")
        notify_owner(f"رسالة مجهولة جديدة #{msg_db_id}", lines, msg, uid)
    say(uid, card(THEME["ok"], "تم إرسال رسالتك", ["وصلت بشكل مجهول تماماً."]))


def handle_reply(tg_user, msg, message_id):
    """يوصل رد المستلم إلى المرسل الأصلي مجهولاً، ويرد على رسالته الأصلية."""
    uid = tg_user["id"]
    original = find_message_for_recipient(uid, message_id)
    if original is None:
        clear_reply_mode(uid)
        return say(uid, card(THEME["warn"], "تعذّر الرد", ["هذه الرسالة لم تعد متاحة."]))
    if uid != OWNER_ID and is_paused():
        return say(uid, paused_card())
    text = msg.get("text")
    if text is not None and len(text) > MAX_TEXT_CHARS:
        return say(uid, too_long_card())
    sender_id = original["sender_id"]
    if is_banned(sender_id):
        clear_reply_mode(uid)
        return say(uid, card(THEME["ban"], "تعذّر الرد", ["لا يمكن إيصال الرد لهذا المرسل."]))

    reply_to = original["sender_msg_id"]
    if text is not None:
        ok = say(sender_id, reply_card(esc(text)), reply_to=reply_to) is not None
    else:
        header = say(sender_id, reply_card("⬇️ المحتوى بالأسفل"), reply_to=reply_to)
        ok = header is not None and copy_message(sender_id, uid, msg["message_id"]) is not None
    if not ok:
        return say(uid, card(THEME["warn"], "تعذّر إرسال ردك", ["حاول لاحقاً."]))

    clear_reply_mode(uid)
    if uid != OWNER_ID:
        original_sender = find_user_by_id(sender_id)
        lines = [
            f"↩️ المستلم (الرد): {who(tg_user.get('first_name'), tg_user.get('username'), uid)}",
            f"👤 المرسل الأصلي: {who(original_sender['first_name'], original_sender['username'], sender_id)}",
            f"🧾 الرسالة الأصلية: #{original['id']}",
        ]
        notify_owner("رد مجهول جديد", lines, msg, uid)
    say(uid, card(THEME["ok"], "تم إرسال ردك", ["وصل للمرسل بشكل مجهول."]))


def handle_callback(cq):
    uid = cq["from"]["id"]
    cq_id = cq["id"]
    data = cq.get("data") or ""
    if is_banned(uid):
        return answer_callback(cq_id, "أنت محظور")
    if not (data.startswith("r:") and data[2:].isdigit()):
        return answer_callback(cq_id)
    message_id = int(data[2:])
    if find_message_for_recipient(uid, message_id) is None:
        return answer_callback(cq_id, "هذه الرسالة لم تعد متاحة")
    set_reply_mode(uid, message_id)
    answer_callback(cq_id, "اكتب ردك الآن ✍️")
    say(uid, card(THEME["write"], "اكتب ردك الآن",
                  ["سيصل للمرسل بشكل مجهول تماماً.", "لإلغاء الرد: /cancel"]))


def handle_command(user, text):
    uid = user["user_id"]
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    if cmd == "/start":
        if args:
            return open_link(user, clean_code(args[0]))
        return send_welcome(user)
    if cmd == "/cancel":
        clear_pending(uid)
        clear_reply_mode(uid)
        return say(uid, card(THEME["ok"], "تم الإلغاء", ["لا توجد عملية إرسال أو رد نشطة."]))
    if uid == OWNER_ID and cmd in OWNER_COMMANDS:
        return OWNER_COMMANDS[cmd](args)
    return send_welcome(user)


def handle_message(msg):
    if msg.get("chat", {}).get("type") != "private":
        return  # يتجاهل المجموعات والقنوات
    tg_user = msg["from"]
    uid = tg_user["id"]
    user = register_user(tg_user)

    if is_banned(uid):
        return say(uid, card(THEME["ban"], "تم حظرك", ["لا يمكنك استخدام البوت."]))

    text = msg.get("text") or ""
    if text.startswith("/"):
        return handle_command(user, text)

    swiped = (msg.get("reply_to_message") or {}).get("message_id")
    if swiped is not None:
        target = find_delivery_message_id(uid, swiped)
        if target is not None:
            return handle_reply(tg_user, msg, target)

    rm = get_reply_mode(uid)
    if rm is not None:
        return handle_reply(tg_user, msg, rm)

    deliver(msg, tg_user)


# ----------------------------------------------------------- أوامر المالك
def cmd_stats(args):
    s = get_stats()
    channel = get_setting("channel", "")
    channel_line = f"<code>{esc(channel)}</code>" if channel else "غير محددة"
    auto_line = "مفعّل ✅" if autopublish_on() else "متوقف ⏸"
    state_line = "متوقف مؤقتاً ⏸" if is_paused() else "يعمل ✅"
    say(OWNER_ID, card(THEME["owner"], "لوحة المالك", [
        f"👥 المستخدمون: <b>{s['users']}</b>",
        f"✉️ الرسائل المجهولة: <b>{s['messages']}</b>",
        f"🚫 المحظورون: <b>{s['banned']}</b>",
        f"{THEME['channel']} القناة: {channel_line}",
        f"🔁 النشر التلقائي: {auto_line}",
        f"⚙️ الاستقبال: {state_line}",
    ]))


def cmd_ban(args, banned):
    if not args or not args[0].isdigit():
        usage = "<code>/ban ID</code>" if banned else "<code>/unban ID</code>"
        return say(OWNER_ID, card(THEME["warn"], "الاستخدام", [usage]))
    target = int(args[0])
    if target == OWNER_ID:
        return say(OWNER_ID, card(THEME["warn"], "لا يمكن حظر المالك"))
    set_ban(target, banned)
    if banned:
        say(OWNER_ID, card(THEME["ban"], "تم الحظر", [f"ID <code>{target}</code>"]))
    else:
        say(OWNER_ID, card(THEME["ok"], "تم رفع الحظر", [f"ID <code>{target}</code>"]))


def cmd_pause(args):
    set_setting("paused", "1")
    say(OWNER_ID, card(THEME["pause"], "تم إيقاف الاستقبال"))


def cmd_resume(args):
    set_setting("paused", "0")
    say(OWNER_ID, card(THEME["ok"], "تم تفعيل الاستقبال"))


def cmd_setchannel(args):
    if not args:
        return say(OWNER_ID, card(THEME["warn"], "الاستخدام", [
            "<code>/setchannel @اسم_القناة</code>",
            "<code>/setchannel off</code> لإلغاء القناة",
        ]))
    value = args[0]
    if value.lower() == "off":
        set_setting("channel", "")
        set_setting("autopublish", "0")
        return say(OWNER_ID, card(THEME["channel"], "تم إلغاء القناة", ["أُوقف النشر التلقائي أيضاً."]))
    valid = (value.startswith("@") and len(value) > 1) or (value.startswith("-100") and value[1:].isdigit())
    if not valid:
        return say(OWNER_ID, card(THEME["warn"], "صيغة غير صحيحة", [
            "استخدم اسم القناة مثل <code>@news</code> أو معرّفها <code>-100...</code>",
        ]))
    set_setting("channel", value)
    say(OWNER_ID, card(THEME["channel"], "تم تحديد القناة", [
        f"<code>{esc(value)}</code>",
        "تأكد أن البوت مشرف في القناة مع صلاحية النشر.",
    ]))


def cmd_autopublish(args):
    mode = args[0].lower() if args else ""
    if mode not in ("on", "off"):
        return say(OWNER_ID, card(THEME["warn"], "الاستخدام", [
            "<code>/autopublish on</code> أو <code>/autopublish off</code>",
        ]))
    if mode == "on" and not get_setting("channel", ""):
        return say(OWNER_ID, card(THEME["warn"], "حدد القناة أولاً", [
            "استخدم <code>/setchannel @اسم_القناة</code>",
        ]))
    set_setting("autopublish", "1" if mode == "on" else "0")
    status = "مفعّل ✅" if mode == "on" else "متوقف ⏸"
    say(OWNER_ID, card(THEME["channel"], f"النشر التلقائي {status}"))


def cmd_users(args):
    page = int(args[0]) if args and args[0].isdigit() else 1
    rows, total = get_users_page(page)
    if total == 0:
        return say(OWNER_ID, card(THEME["warn"], "لا يوجد مستخدمون بعد"))
    if not rows:
        return say(OWNER_ID, card(THEME["warn"], "لا توجد نتائج لهذه الصفحة"))
    per_page = 15
    pages = (total + per_page - 1) // per_page
    lines = []
    for r in rows:
        mark = f" {THEME['ban']}" if is_banned(r["user_id"]) else ""
        lines.append(f"{who(r['first_name'], r['username'], r['user_id'])}"
                     f" · كود <code>{esc(r['code'])}</code>{mark}")
    footer = f"صفحة {page}/{pages} · الإجمالي {total}"
    if page < pages:
        footer += f" — التالي: <code>/users {page + 1}</code>"
    say(OWNER_ID, card(THEME["owner"], "مستخدمو البوت", lines, footer=footer))


def cmd_setcolor(args):
    if not args:
        names = "، ".join(["بنفسجي", "أخضر", "أزرق", "أحمر", "برتقالي", "أصفر", "أسود", "أبيض", "بني"])
        return say(OWNER_ID, card(THEME["warn"], "الاستخدام", [
            "<code>/setcolor اسم_اللون</code>",
            f"الألوان المتاحة: {names}",
        ]))
    name = args[0].strip()
    emoji = COLOR_PALETTE.get(name, name if len(name) <= 4 and not name.isalnum() else None)
    if emoji is None:
        return say(OWNER_ID, card(THEME["warn"], "لون غير معروف", [
            "جرّب: بنفسجي، أخضر، أزرق، أحمر، برتقالي، أصفر، أسود، أبيض، بني",
        ]))
    set_setting("incoming_color", emoji)
    say(OWNER_ID, card(emoji, "تم تغيير لون الرسائل الواردة", [f"الرمز الجديد: {emoji}"]))


def cmd_help(args):
    say(OWNER_ID, card(THEME["owner"], "أوامر المالك", [
        "<code>/stats</code> — لوحة المالك",
        "<code>/users [صفحة]</code> — عرض مستخدمي البوت",
        "<code>/ban ID</code> و <code>/unban ID</code> — حظر ورفع الحظر",
        "<code>/setchannel @القناة</code> — تحديد قناة النشر",
        "<code>/setchannel off</code> — إلغاء القناة",
        "<code>/autopublish on</code> أو <code>off</code> — النشر التلقائي",
        "<code>/setcolor اسم_اللون</code> — لون الرسائل الواردة",
        "<code>/pause</code> و <code>/resume</code> — إيقاف وتفعيل الاستقبال",
    ]))


OWNER_COMMANDS = {
    "/stats": cmd_stats,
    "/users": cmd_users,
    "/ban": lambda args: cmd_ban(args, True),
    "/unban": lambda args: cmd_ban(args, False),
    "/pause": cmd_pause,
    "/resume": cmd_resume,
    "/setchannel": cmd_setchannel,
    "/autopublish": cmd_autopublish,
    "/setcolor": cmd_setcolor,
    "/help": cmd_help,
}


# -------------------------------------------------------------- التشغيل
def main():
    global BOT_USERNAME
    if not BOT_TOKEN:
        sys.exit("⚠️ لم يتم تعيين متغير البيئة BOT_TOKEN.")
    init_db()
    me = tg("getMe")
    if not me:
        sys.exit("⚠️ تعذّر الاتصال بتيليغرام. تحقق من التوكن والإنترنت.")
    BOT_USERNAME = me["username"]
    log.info("البوت يعمل: @%s", BOT_USERNAME)

    offset = None
    while True:
        updates = tg(
            "getUpdates",
            http_timeout=POLL_TIMEOUT + 10,
            timeout=POLL_TIMEOUT,
            offset=offset,
            allowed_updates=json.dumps(["message", "callback_query"]),
        )
        if updates is None:
            time.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                if "message" in update:
                    handle_message(update["message"])
                elif "callback_query" in update:
                    handle_callback(update["callback_query"])
            except Exception:
                log.exception("خطأ أثناء معالجة التحديث %s", update.get("update_id"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("تم إيقاف البوت.")
