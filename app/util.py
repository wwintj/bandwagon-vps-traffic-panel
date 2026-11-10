"""Small shared helpers: formatting, timing, blocking-call offload."""
import asyncio
import html
import logging
import platform
from datetime import datetime

from app import timeutil


logger = logging.getLogger("uvicorn.error")


async def run_blocking(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args))


def get_os_pretty_name():
    try:
        with open('/etc/os-release', 'r') as f:
            for line in f:
                if line.startswith('PRETTY_NAME='):
                    return line.split('=', 1)[1].strip().strip('"')
    except Exception:
        pass
    return f"{platform.system()} {platform.release()}"


def format_meta_time(value):
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def is_safe_auth_value(value):
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@_.!%-")
    return bool(value) and all(ch in allowed for ch in value)


def to_traditional_text(value):
    phrase_map = {
        "服务器": "伺服器",
        "服务": "服務",
        "监控": "監控",
        "流量": "流量",
        "面板": "面板",
        "台湾": "臺灣",
        "后台": "後台",
        "现在": "現在",
        "这里": "這裡",
        "这个": "這個",
        "那个": "那個",
        "设置": "設定",
    }
    char_map = str.maketrans({
        "监": "監", "控": "控", "务": "務", "器": "器", "汉": "漢", "简": "簡", "体": "體",
        "国": "國", "东": "東", "广": "廣", "韩": "韓",
        "龙": "龍", "马": "馬", "门": "門", "云": "雲", "电": "電", "脑": "腦", "网": "網",
        "页": "頁", "题": "題", "标": "標", "副": "副", "时": "時", "间": "間", "运": "運",
        "行": "行", "总": "總", "传": "傳", "载": "載", "数": "數", "据": "據", "统": "統",
        "计": "計", "节": "節", "点": "點", "后": "後", "台": "臺", "机": "機", "场": "場",
        "区": "區", "设": "設", "置": "置", "开": "開", "关": "關", "启": "啟", "动": "動",
        "态": "態", "显": "顯", "示": "示", "维": "維", "护": "護", "者": "者", "密": "密",
        "码": "碼", "用": "用", "户": "戶", "名": "名", "登": "登", "录": "錄", "链": "鏈",
        "接": "接", "实": "實", "测": "測", "试": "試", "线": "線", "宽": "寬", "带": "帶",
        "现": "現", "这": "這", "个": "個", "为": "為", "发": "發", "过": "過", "会": "會",
        "与": "與", "对": "對", "归": "歸", "类": "類", "项": "項", "选": "選", "择": "擇",
        "认": "認", "证": "證", "书": "書", "删": "刪", "除": "除", "读": "讀", "写": "寫",
        "画": "畫", "图": "圖", "档": "檔", "锁": "鎖", "长": "長", "应": "應", "当": "當",
        "处": "處", "弹": "彈", "窗": "窗", "关": "關", "闭": "閉", "错": "錯", "误": "誤",
        "压": "壓", "缩": "縮", "径": "徑", "验": "驗", "坏": "壞",
        "导": "導", "页": "頁", "进": "進", "级": "級", "单": "單", "双": "雙", "临": "臨",
        "钟": "鐘", "块": "塊", "仅": "僅", "该": "該", "从": "從", "无": "無",
        "优": "優", "侧": "側", "栏": "欄", "号": "號", "软": "軟", "硬": "硬", "盘": "盤",
    })
    text = str(value or "")
    for source, target in phrase_map.items():
        text = text.replace(source, target)
    return text.translate(char_map)


def format_bytes(value):
    value = int(value or 0)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(value)
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    return f"{size:.2f} {units[unit_index]}"


def escape_html(value):
    return html.escape(str(value), quote=False)


def format_unix_time(value):
    try:
        timestamp = int(value)
        if timestamp <= 0:
            return ""
        return timeutil.from_unix(timestamp).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return str(value or "")


def configured_now():
    return timeutil.now()
