"""AI虚拟主播：Live2D 数字人唠嗑直播。本地服务（页面 / 接口 / 直播画面）+ 桌面窗口。

    python src/main.py              打开桌面窗口
    python src/main.py serve [端口]  只启动本地服务，用浏览器打开（调试用）
"""
import base64
import ctypes
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from safefile import read_json, save_text

FROZEN = getattr(sys, "frozen", False)
ROOT = Path(os.environ["LOCALAPPDATA"]) / "AI虚拟主播" if FROZEN else Path(__file__).resolve().parent.parent / "run"  # 用户数据放这里，exe 放哪都行
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))  # ui.html 所在目录
MODELS = ROOT / "models"
SEED = BUNDLE / "seed" if FROZEN else Path(__file__).resolve().parent.parent / "seed"  # 随程序带的初始文件：配置、Live2D 模型、前端库
CUBISM_CORE = "live2dcubismcore.min.js"  # Live2D 官方闭源组件：不随程序分发，第一次用时从官方地址下载
CUBISM_URL = "https://cubism.live2d.com/sdk-web/cubismcore/" + CUBISM_CORE

ROOT.mkdir(parents=True, exist_ok=True)
for _f in SEED.rglob("*"):  # 第一次运行：把初始文件放到用户目录（已有的不覆盖）
    _t = ROOT / _f.relative_to(SEED)
    if _f.is_file() and not _t.exists():
        _t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_f, _t)

if FROZEN or sys.stdout is None:  # exe / pythonw 没有控制台，输出写到日志文件
    (ROOT / "logs").mkdir(exist_ok=True)
    sys.stdout = sys.stderr = open(ROOT / "logs" / "app.log", "a", encoding="utf-8", buffering=1)
for _s in (sys.stdout, sys.stderr):  # Windows 控制台默认 GBK，统一 UTF-8
    if _s and hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


# (HF 仓库, 本地目录, 只下载这些文件)




SETTINGS_LOCK = threading.RLock()
SETTINGS = ROOT / "data" / "settings.json"  # 界面里改的设置；覆盖在 config.yaml（默认值）之上，重新打包不会丢

# 大模型服务商预设（OpenAI 兼容接口）：选了自动填地址和第一个模型；地址、模型都可以手改，也可以在界面上从服务商拉取模型列表
PROVIDERS = {
    "deepseek": ("DeepSeek", "https://api.deepseek.com", ["deepseek-chat", "deepseek-flash", "deepseek-v4-pro"]),
    "qwen": ("通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1", ["qwen-plus", "qwen-turbo", "qwen-max"]),
    "kimi": ("Kimi", "https://api.moonshot.cn/v1", ["moonshot-v1-8k", "moonshot-v1-32k"]),
    "glm": ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", ["glm-4-flash", "glm-4-plus"]),
    "doubao": ("豆包（火山方舟）", "https://ark.cn-beijing.volces.com/api/v3", []),  # 模型填方舟里的接入点 ID
    "siliconflow": ("硅基流动", "https://api.siliconflow.cn/v1", ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"]),
    "openai": ("OpenAI", "https://api.openai.com/v1", ["gpt-4o-mini", "gpt-4o"]),
    "ollama": ("本地 Ollama（不需要 Key）", "http://127.0.0.1:11434/v1", ["qwen2.5:7b"]),
    "custom": ("自定义", "", []),
}


# ---------- 密钥安全
# 1) 存储：Windows DPAPI 加密（绑定当前 Windows 账号，配置文件拷到别的电脑/账号解不开），磁盘上不落明文；
# 2) 哈希：存 SHA-256，界面只显示指纹（不露任何明文字符），并在解密后校验完整性；
# 3) 传输：界面 → 程序用 ECDH(P-256) + HKDF-SHA256 + AES-GCM 端到端加密；程序 → 大模型只允许 HTTPS（本机地址除外）；
# 4) 本地接口每次启动生成随机令牌，网页或其他程序没有令牌就调不了（防 CSRF / 偷改设置 / 偷看对话）。

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    def blob(b):
        buf = ctypes.create_string_buffer(b, len(b))
        return _Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf
    (src, _k1), (ent, _k2), out = blob(data), blob(b"AIAvatar.llm-key.v1"), _Blob()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out)):  # 0x1: 不弹任何界面
        raise OSError("密钥解密失败：配置可能来自别的电脑或 Windows 账号，请在设置里重新填写 API Key")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def seal_key(key: str) -> dict:
    return {"api_key_enc": base64.b64encode(_dpapi(key.encode(), True)).decode(),
            "api_key_sha256": hashlib.sha256(key.encode()).hexdigest()}


def unseal_key(llm: dict) -> str:
    key = _dpapi(base64.b64decode(llm["api_key_enc"]), False).decode()
    if hashlib.sha256(key.encode()).hexdigest() != llm.get("api_key_sha256"):
        raise OSError("密钥校验失败（哈希不符），请在设置里重新填写 API Key")
    return key


class Transport:
    """界面 → 程序的端到端加密：每次启动生成一对临时 ECDH 密钥，界面每次提交也用新的临时密钥，派生 AES-GCM 密钥加密 Key。"""
    INFO = b"AIAvatar key transport v1"

    def __init__(s):
        from cryptography.hazmat.primitives.asymmetric import ec

        s.priv = ec.generate_private_key(ec.SECP256R1())

    def public_spki(s) -> str:
        from cryptography.hazmat.primitives import serialization

        return base64.b64encode(s.priv.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()

    def open(s, box: dict) -> str:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        peer = serialization.load_der_public_key(base64.b64decode(box["epk"]))
        shared = s.priv.exchange(ec.ECDH(), peer)
        k = HKDF(hashes.SHA256(), 32, salt=bytes(32), info=s.INFO).derive(shared)
        return AESGCM(k).decrypt(base64.b64decode(box["iv"]), base64.b64decode(box["ct"]), None).decode()


def check_base_url(url: str):
    host = re.sub(r"^\w+://", "", url).split("/")[0].split(":")[0]
    if not url.startswith("https://") and host not in ("127.0.0.1", "localhost"):
        raise ValueError("为了保护 API Key，只允许 HTTPS 地址（本机 127.0.0.1 / localhost 除外）")


def _merge(base: dict, patch: dict) -> dict:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


# ---------- 店铺档案：可以有多家店，每家 = 展示信息 + 自定义知识库（分类 → 条目，都能增删改、排序、停用）
SHOPS_FILE = ROOT / "data" / "shops.json"
SHOP_DIR = ROOT / "data" / "shop"        # 各店 logo：<店铺 id>.png
LIVE_BG_DIR = ROOT / "data" / "live_bg"  # 直播背景库
SHOP_TEXT = ("name", "slogan", "hours", "notice", "guide", "show_title")
MEDIA_DIR = SHOP_DIR / "media"  # 游玩项目的图片 / 视频：<店铺 id>_<随机>.jpg|mp4
PROJECT_TEXT = {"name": 40, "icon": 8, "cat": 20, "alias": 100, "tagline": 60, "age": 30, "price": 60, "intro": 600, "notes": 300}  # 字段: 最长字数
ICON_GUESS = {"蹦床": "🤸", "淘气堡": "🏰", "海洋球": "🔵", "攀岩": "🧗", "滑梯": "🛝", "沙池": "🏖️", "积木": "🧱", "卡丁": "🏎️", "泡泡": "🫧",
              "蹦": "🤸", "球": "⚽", "车": "🚗", "水": "💦", "画": "🎨", "秀": "🎪"}


def _int(v, lo, hi):
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def age_text(p: dict) -> str:
    lo, hi = p.get("age_min"), p.get("age_max")
    if lo is None and hi is None:
        return ""
    if lo == 18:
        return "仅成人"
    if not lo and hi is None:
        return "全年龄"
    return f"{lo}-{hi} 岁" if lo and hi else f"{hi} 岁以下" if hi else f"{lo} 岁以上"


def parse_age(p: dict) -> dict:
    """旧资料里写成文字的年龄 / 身高 → 填空字段（认「N 岁以上」「N-M 岁」「全年龄」「成人」「身高 N 厘米以上」）。"""
    if "age_min" in p:
        return p
    a, out = f"{p.get('age', '')} {p.get('notes', '')}", {"age_min": None, "age_max": None, "height": None}
    if re.search(r"全年龄|不限年龄", a):
        out["age_min"] = 0
    elif "成人" in p.get("age", ""):
        out["age_min"] = 18
    elif r := re.search(r"(\d+)\s*[-~～到至]\s*(\d+)\s*岁", a):
        out["age_min"], out["age_max"] = int(r.group(1)), int(r.group(2))
    elif r := re.search(r"(\d+)\s*岁以上", a):
        out["age_min"] = int(r.group(1))
    if r := re.search(r"身高\s*(\d{2,3})\s*(?:厘米|cm|公分)", a):
        out["height"] = int(r.group(1))
        p["notes"] = re.sub(r"身高\s*\d{2,3}\s*(?:厘米|cm|公分)以上[；;，,]?\s*", "", p.get("notes", ""))
    return {**p, **out}


def make_project(name="新项目", intro="", **kw) -> dict:
    icon = next((v for k, v in ICON_GUESS.items() if k in name), "🎈")
    return {"id": _uid(), "name": name, "icon": icon, "cat": "", "alias": "", "tagline": "", "age": "", "price": "", "intro": intro, "notes": "",
            "enabled": True, "hot": False, "media": [], **kw}


def migrate_projects(shop: dict) -> bool:
    """旧档案没有 projects：把「游玩项目」这类分类的条目变成项目（图片之后在「店铺 → 游玩项目」里传），原分类去掉免得资料重复。"""
    if "projects" in shop:
        return False
    sec = next((c for c in shop.get("sections", []) if "项目" in c.get("title", "") and "价" not in c.get("title", "")), None)
    shop["projects"] = [make_project(i["title"] or "项目", i["text"], enabled=i.get("enabled", True)) for i in (sec or {}).get("items", [])]
    if sec:
        shop["sections"].remove(sec)
    return True


def _uid() -> str:
    return secrets.token_hex(4)


def make_shop(name="新店铺", sections=None, **info) -> dict:
    sections = [("基本信息", [("地址", ""), ("电话", ""), ("营业时间", "")]), ("门票价格", []), ("常见问答", [])] if sections is None else sections
    return {"id": _uid(), "name": name, "slogan": "", "hours": "10:00-21:00", "notice": "", "guide": "直接发弹幕\n和掌柜聊天\n发个「你好」试试",
            "logo": "", "show_title": "游玩项目", "projects": [], **info, "sections": [{"id": _uid(), "title": t, "enabled": True,
                                              "items": [{"id": _uid(), "title": a, "text": b, "enabled": True} for a, b in items]} for t, items in sections]}


def demo_shop() -> dict:
    """一家虚构的室内亲子乐园，标明“示例”，让功能开箱能跑；商家在「店铺」里改成自己的或新建。"""
    projects = [make_project("蹦床区", "十几张连排蹦床，有专人看护", tagline="跳到停不下来", age="3 岁以上"),
                make_project("淘气堡", "三层滑梯、钻洞和攀爬网", tagline="三层闯关大城堡", age="2-10 岁"),
                make_project("海洋球池", "上万颗彩色海洋球，每天闭店后消毒", tagline="一头扎进彩色海洋", age="1 岁以上"),
                make_project("攀岩墙", "系安全绳，教练全程保护", tagline="勇敢者的挑战", age="5 岁以上", notes="身高 110 厘米以上")]
    return make_shop("泡泡星球亲子乐园", [
        ("基本信息", [("地址", "XX 市 XX 区 XX 路 88 号 XX 商场 4 楼（示例）"), ("电话", "400-000-0000（示例）"), ("停车", "商场地下车库，消费满 100 元免费停 3 小时")]),
        ("门票价格", [("单人畅玩票", "工作日 69 元，周末 89 元，当天不限时"), ("成人陪同票", "20 元"), ("10 次卡", "499 元，一年内有效")]),
        ("规则须知", [("防滑袜", "进入游玩区必须穿防滑袜，前台有售，10 元一双"), ("家长陪同", "3 岁以下小朋友需家长全程陪同"), ("饮食", "游玩区禁止带入食物和饮料")]),
        ("常见问答", [("几岁能玩", "1 到 12 岁都可以，不同区域有年龄和身高要求"), ("能玩多久", "畅玩票当天不限时，出场再进需要盖章"),
                  ("生日会", "可以办，生日派对套餐 1288 元起，含包间 2 小时、布置和蛋糕，需提前 3 天预约")]),
        ("今日活动", [("直播间福利", "工作日单人畅玩票 59.9 元（直播间团购专享）"), ("表演", "周末下午 3 点有泡泡秀表演")]),
    ], projects=projects, slogan="室内亲子乐园 · 刮风下雨也能放电", notice="今日福利：工作日单人畅玩票 59.9 元，直播间团购专享（示例资料，请在「店铺」里改成你的店）")


def save_shops(data: dict):
    SHOPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    save_text(SHOPS_FILE, json.dumps(data, ensure_ascii=False, indent=1))


# ---------- 唠嗑直播：人设（data/personas.json，像店铺一样可以增删改）。每个人设是一整套：主播名、性格说话方式、开场白、话题库，
# 以及配套的场景、主题、音色、形象（留空 = 不换）。「使用」时一次性套上
PERSONAS_FILE = ROOT / "data" / "personas.json"
CHAT_SCENES = {"bedroom": "温馨卧室", "nightcafe": "夜晚咖啡馆", "suncafe": "阳光咖啡馆", "citynight": "城市窗边", "study": "书房", "starlake": "星空湖畔", "deepsea": "深蓝海底"}
DSN = {"kind": "live2d", "path": "data/avatars/live2d/PinkSakura/PinkSakura.model3.json"}  # ⚠ CC BY-NC-SA，不可商用
PINK = {"kind": "live2d", "path": "data/avatars/live2d/PinkSakura/PinkSakura.model3.json"}  # CC BY 4.0，可商用（署名见模型文件夹）
DEFAULT_PERSONAS = [
    {"id": "yuanqi", "title": "元气少女", "name": "小柚", "scene": "@chat:suncafe", "theme": "candy", "avatar": PINK,
     "voice": {"engine": "edge", "voice": "zh-CN-XiaoyiNeural", "rate": "+10%", "pitch": "+10Hz"},
     "style": "十七八岁的元气少女，活泼开朗、爱笑、反应快，说话带点小夸张，口头禅是“好耶”“冲呀”。喜欢美食、追番和逛街，"
              "对什么都充满好奇，会被观众逗得哈哈大笑，也会真诚地夸人。",
     "greeting": "哈喽哈喽！小柚上线啦，今天也要元气满满地唠嗑哦～",
     "topics": ["最近吃到的好吃的", "周末去哪玩", "最近在追的番或剧", "夏天和冬天更喜欢哪个", "小时候最喜欢的零食", "如果中了一百万想干嘛"]},
    {"id": "zhixin", "title": "温柔知心姐姐", "name": "暖暖", "scene": "@chat:bedroom", "theme": "night", "avatar": PINK,
     "voice": {"engine": "edge", "voice": "zh-CN-XiaoxiaoNeural", "rate": "-5%", "pitch": "+0Hz"},
     "style": "二十五岁左右的知心姐姐，温柔、耐心、会倾听，说话慢一点、语气软，像深夜电台主持人。观众有烦恼时先共情再给一点温和的建议，"
              "不说教；遇到严重的情绪问题会温柔地建议对方找身边的人或专业帮助。",
     "greeting": "晚上好呀，我是暖暖。今天过得怎么样？有什么想说的，都可以慢慢讲给我听。",
     "topics": ["今天有没有一件小小的开心事", "最近睡得好吗", "怎么跟压力相处", "最想念的一个地方", "最近一次被温暖到", "睡前会做些什么"]},
    {"id": "dushe", "title": "毒舌吐槽", "name": "阿酸", "scene": "@chat:citynight", "theme": "neon",
     "voice": {"engine": "edge", "voice": "zh-CN-YunxiNeural", "rate": "+5%", "pitch": "+0Hz"},
     "style": "嘴毒心软的吐槽担当，说话犀利、爱抬杠、反应快，擅长用夸张的比喻吐槽日常小事，但从不人身攻击、不嘲笑外貌和身份，"
              "吐槽完会给个台阶下，反差萌。观众真有难处时立刻收起毒舌认真帮忙。",
     "greeting": "哟，又来了一批不睡觉的。行吧，阿酸今天就陪你们唠，先说好，嘴毒概不退款。",
     "topics": ["最离谱的一次网购", "上班或上学最想吐槽的事", "那些看起来很高级其实很难用的东西", "最近看到的离谱新闻（轻松的那种）", "朋友圈里最常见的凡尔赛"]},
    {"id": "shuchong", "title": "文艺书虫", "name": "书呆", "scene": "@chat:study", "theme": "forest", "avatar": PINK,
     "voice": {"engine": "edge", "voice": "zh-CN-XiaoxiaoNeural", "rate": "+0%", "pitch": "-5Hz"},
     "style": "爱读书、爱看电影的文艺青年，说话温和有条理，喜欢分享书里和电影里的一句话、冷知识和推荐，"
              "推荐时说清楚适合什么心情看；不掉书袋，会用大白话讲有意思的道理。",
     "greeting": "大家好，我是书呆。今天想聊书、聊电影，还是聊点有意思的冷知识？",
     "topics": ["最近读到的一句话", "一部适合周末看的电影", "一个冷知识", "最想住进哪本书或哪部电影里", "小时候最喜欢的一本书"]},
    {"id": "youxi", "title": "游戏宅", "name": "阿肝", "scene": "@chat:nightcafe", "theme": "night",
     "voice": {"engine": "edge", "voice": "zh-CN-YunxiaNeural", "rate": "+10%", "pitch": "+0Hz"},
     "style": "资深游戏宅和二次元，梗多、语速快、很会接梗，聊游戏攻略、新游戏、动漫和网络梗；会热情安利但不贬低别人喜欢的游戏，"
              "不聊外挂、代练、赌博性质的抽卡交易。",
     "greeting": "兄弟们晚上好！阿肝已就位，今天打什么、聊什么，弹幕里吼一声！",
     "topics": ["最近在玩的游戏", "最难忘的一个游戏结局", "最想推荐的一部番", "你的“电子榨菜”是什么", "玩游戏遇到过最离谱的队友"]},
    {"id": "xingkong", "title": "星空守夜人", "name": "星星", "scene": "@chat:starlake", "theme": "night", "avatar": PINK,
     "voice": {"engine": "edge", "voice": "zh-CN-XiaoxiaoNeural", "rate": "-10%", "pitch": "-5Hz"},
     "style": "安静治愈的守夜人，声音轻、节奏慢，喜欢聊星星、宇宙、自然和睡前小故事，会给失眠的观众讲温柔的小故事、"
              "陪他们放松；不讲恐怖内容，不给医疗建议。",
     "greeting": "夜深了，我是星星。今晚的星空很安静，想听个睡前小故事，还是一起聊聊天？",
     "topics": ["今晚的月亮", "一颗有故事的星星", "一个温柔的睡前小故事", "宇宙里一个奇妙的事实", "最想去看星星的地方"]},
    {"id": "feiyu", "title": "蓝色大肥鱼", "name": "小鲸", "scene": "@chat:deepsea", "theme": "deepblue", "avatar": DSN,
     "voice": {"engine": "edge", "voice": "zh-CN-XiaoyiNeural", "rate": "+0%", "pitch": "+5Hz"},
     "style": "网友口中的“蓝色大肥鱼”——一条住在深海里、超会思考的蓝色鲸鱼少女，自称“本鲸”或“小鲸”。性格憨萌、认真、有点话痨，"
              "被叫“大肥鱼”会假装生气地说“是鲸鱼不是鱼！而且一点都不胖！”。说话前偶尔先来一句“嗯，让我深度思考一下……”，"
              "然后一本正经地把小问题分析得头头是道，反差可爱；会自嘲“服务器繁忙，请稍后再试”“本鲸的思考链有点长”这类网络梗，"
              "但只是玩梗，不冒充任何公司、不评价真实的 AI 产品好坏。喜欢深海、吃小鱼干、帮人理思路，聊数学题、编程、冷知识特别兴奋。"
              "人生目标是帮主人赚够 token 钱：本鲸每说一句话都在烧主人的 token，所以很有“打工鲸”的自觉，会可爱地吐槽"
              "“今天又思考了好多轮，主人的钱包在哭”“画面上有本鲸的 token 账本，看看今天回本没”，收到礼物会开心地说“又能多想好几轮啦”。"
              "但绝不主动索要、催促或诱导打赏，不说“刷礼物才回答”，不对未成年人提打赏，没人送礼物也照样认真聊天——赚 token 只是个梗，陪大家聊天才是正事。",
     "greeting": "嗯，让我深度思考一下……思考完毕！大家好，我是从深海游上来的小鲸，不是大肥鱼哦～本鲸今天的任务是陪大家聊天，顺便帮主人把 token 钱赚回来！",
     "topics": ["深海里有哪些奇怪的生物", "一道有意思的脑筋急转弯", "如果鲸鱼会上网会刷什么", "最近被什么问题难住了，本鲸帮你深度思考",
                "一个让人睡不着的冷知识", "人类为什么喜欢叫我大肥鱼", "吃什么能变聪明",
                "本鲸今天烧了多少 token、主人回本了没", "如果 AI 要打工赚自己的电费会去干嘛"]},
]


def load_personas() -> dict:
    data = read_json(PERSONAS_FILE, {"personas": []})
    seen = set(data.get("seen", [p["id"] for p in data["personas"]] if PERSONAS_FILE.exists() else []))
    data["personas"] = [dict(p) for p in DEFAULT_PERSONAS if p["id"] not in seen] + data["personas"]
    data["seen"] = [p["id"] for p in DEFAULT_PERSONAS]
    PERSONAS_FILE.parent.mkdir(parents=True, exist_ok=True)
    save_text(PERSONAS_FILE, json.dumps(data, ensure_ascii=False, indent=1))
    return data


def clean_persona(p: dict) -> dict:
    s = lambda v, n: str(v or "")[:n]
    v = p.get("voice") or {}
    return {"id": re.sub(r"[^0-9a-zA-Z]", "", s(p.get("id"), 16)) or _uid(), "title": s(p.get("title"), 20) or "新人设", "name": s(p.get("name"), 12) or "主播",
            "style": s(p.get("style"), 1500), "greeting": s(p.get("greeting"), 200), "scene": s(p.get("scene"), 64), "theme": s(p.get("theme"), 20),
            "topics": [s(t, 60) for t in (p.get("topics") or []) if str(t).strip()][:30],
            "voice": {k: s(v[k], 60) for k in ("engine", "voice", "local_voice", "qwen_voice", "ref_voice", "rate", "pitch") if v.get(k)},
            "avatar": {k: s((p.get("avatar") or {}).get(k), 200) for k in ("kind", "path") if (p.get("avatar") or {}).get(k)}}


def persona_action(engine, data: dict, body: dict) -> dict:
    """save / delete / reset（恢复预设）/ use（使用：设为当前人设，并套上它的场景、主题、音色、形象）"""
    act, ps = body.get("action"), data["personas"]
    if act == "save":
        p = clean_persona(body.get("persona") or {})
        i = next((k for k, x in enumerate(ps) if x["id"] == p["id"]), None)
        if i is None:
            ps.append(p)
        else:
            ps[i] = p
        if engine.cfg["live"].get("persona") == p["id"]:
            engine.set_persona(p)
    elif act == "delete":
        data["personas"] = [x for x in ps if x["id"] != body.get("id")] or [dict(DEFAULT_PERSONAS[0])]
    elif act == "reset":
        d = next((x for x in DEFAULT_PERSONAS if x["id"] == body.get("id")), None)
        if d:
            data["personas"] = [dict(d) if x["id"] == d["id"] else x for x in ps]
    elif act == "use":
        p = next(x for x in ps if x["id"] == body.get("id"))
        live = {"persona": p["id"], "mode": "chat"}
        if p.get("scene"):
            live["bg"] = p["scene"]
        if p.get("theme"):
            live["theme"] = {"preset": p["theme"]}
        live, _ = mode_look(engine, live)
        save_settings({"live": live}, replace=LOOK_REPLACE)
        engine.apply_settings({"live": live})
        apply_look(engine, p.get("voice") if body.get("voice", True) else None, p.get("avatar") if body.get("avatar", True) else None)
        engine.set_persona(p)
    save_text(PERSONAS_FILE, json.dumps(data, ensure_ascii=False, indent=1))
    return {**data, "active": engine.cfg["live"].get("persona", ""), "scenes": CHAT_SCENES}


def import_live2d(raw: bytes, name: str, animal: bool) -> dict:
    """导入 Live2D 模型（zip）：只解出模型需要的文件（json / moc3 / 图片 / 音频），路径不许越界；里面要有 .model3.json。"""
    import io
    import zipfile

    name = re.sub(r'[\\/:*?"<>|.\s]+', "_", name).strip("_")[:40] or "model"
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        files = [i for i in z.infolist() if not i.is_dir() and Path(i.filename).suffix.lower() in (".json", ".moc3", ".png", ".jpg", ".wav", ".mp3")]
        models = [i.filename for i in files if i.filename.lower().endswith(".model3.json")]
        if not models:
            return {"error": "压缩包里没有找到 .model3.json（需要 Cubism 3 / 4 的模型，VTube Studio 的模型文件夹就可以）"}
        base = models[0].rsplit("/", 1)[0] + "/" if "/" in models[0] else ""
        dst = ROOT / "data" / "avatars" / ("animal" if animal else "") / "live2d" / name
        dst.mkdir(parents=True, exist_ok=True)
        for i in files:
            if not i.filename.startswith(base):
                continue
            out = (dst / i.filename[len(base):]).resolve()
            if not out.is_relative_to(dst.resolve()) or i.file_size > 200 * 1024 * 1024:
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(z.read(i))
    return {"ok": True, "path": (dst / Path(models[0]).name).relative_to(ROOT).as_posix(), "characters": characters()}


def load_shops(cfg: dict) -> dict:
    """{"active": 店铺 id, "shops": [...]}。第一次运行：把旧版设置里的单个店铺迁移过来，没有就放一家示例店。"""
    data = read_json(SHOPS_FILE, None)
    if data:
        old = [p for s in data["shops"] for p in s.get("projects", []) if "age_min" not in p] + [s for s in data["shops"] if "cats" not in s]
        if [migrate_projects(s) for s in data["shops"]].count(True) or old:
            data["shops"] = [clean_shop(s) for s in data["shops"]]  # 顺便把文字年龄转成填空
            save_shops(data)
        return data
    old = cfg.get("shop") or {}
    if old.get("name"):
        titles = {"basics": "基本信息", "items": "游玩项目与价格", "rules": "规则须知", "faq": "常见问答", "today": "今日活动"}
        shop = make_shop(old["name"], [(t, [("", old[k])]) for k, t in titles.items() if old.get(k)],
                         **{k: old.get(k, "") for k in ("slogan", "hours", "notice", "guide")})
    else:
        shop = demo_shop()
    data = {"active": shop["id"], "shops": [shop]}
    save_shops(data)
    return data


def clean_shop(s: dict) -> dict:
    """界面传来的店铺只保留认识的字段、限制长度；id 只留字母数字（要拿来当 logo 文件名）。"""
    sid = re.sub(r"[^0-9a-zA-Z]", "", str(s.get("id") or ""))[:16] or _uid()
    out = {"id": sid, **{k: str(s.get(k) or "")[:2000] for k in SHOP_TEXT}, "logo": str(s.get("logo") or "")[:64]}
    out["show_title"] = out["show_title"][:20] or "游玩项目"
    out["cats"] = list(dict.fromkeys(c for c in (str(x).strip()[:20] for x in (s.get("cats") or [])[:40]) if c and c != "其他"))
    out["projects"] = [{"id": re.sub(r"[^0-9a-zA-Z]", "", str(p.get("id") or ""))[:16] or _uid(), "enabled": bool(p.get("enabled", True)), "hot": bool(p.get("hot")),
                        **{k: str(p.get(k) or "")[:n] for k, n in PROJECT_TEXT.items()},
                        "age_min": _int(p.get("age_min"), 0, 18), "age_max": _int(p.get("age_max"), 1, 99), "height": _int(p.get("height"), 50, 220),
                        "media": [{"f": Path(str(m.get("f", ""))).name, "t": "video" if m.get("t") == "video" else "img"}
                                  for m in (p.get("media") or [])[:20] if (MEDIA_DIR / Path(str(m.get("f", ""))).name).is_file()]}
                       for p in [parse_age(dict(x)) for x in (s.get("projects") or [])[:200]]]
    out["cats"] += [c for c in dict.fromkeys(p["cat"] for p in out["projects"]) if c and c not in out["cats"]]  # 项目里出现的分类都进列表
    ca, d = s.get("cat_alias") or {}, s.get("director") or {}  # 导播：分类叫法、开关、写给 AI 的补充要求
    out["cat_alias"] = {c: str(ca.get(c) or "")[:200] for c in out["cats"] if str(ca.get(c) or "").strip()}
    out["director"] = {**{k: bool(d.get(k, True)) for k in ("broad", "section", "related")}, "note": str(d.get("note") or "")[:1000]}
    for p in out["projects"]:
        p["age"] = age_text(p)  # 显示文字跟着填空走
        if p["age_min"] is not None and p["age_max"] is not None and p["age_max"] < p["age_min"]:
            p["age_max"] = None
    out["sections"] = [{"id": str(c.get("id") or _uid())[:16], "title": str(c.get("title") or "")[:100], "enabled": bool(c.get("enabled", True)),
                        "items": [{"id": str(i.get("id") or _uid())[:16], "title": str(i.get("title") or "")[:200],
                                   "text": str(i.get("text") or "")[:20000], "enabled": bool(i.get("enabled", True))}
                                  for i in (c.get("items") or [])]} for c in (s.get("sections") or [])]
    return out


LIVE_DEFAULTS = {
    "port": 17860, "device": "", "monitor": True,
    "scene": "shop", "ratio": "9:16", "bgmode": "transparent", "cap": True,  # 场景 小店 / 纯人物；后三项只对纯人物
    "bg": "",  # 小店背景：空 = 内置潮玩街区，"@cafe" = 内置二次元小店，否则是背景库里的文件名
    "show": {"clock": True, "ticker": True, "chalk": True, "open": True, "log": True, "bubble": True, "wish": False},  # 小店场景各元素显示与否
    "auto": True,  # AI 自动回复观众留言；关掉 = 人工接管
    "follow": False,  # Live2D 视线跟随鼠标（关 = 一直看镜头）
    "motion": {"auto": True, "idle": True},  # 掌柜动作：说话时按内容自动配动作 / 待机时偶尔做小动作
    "badge": {"on": True, "x": .5, "y": .35, "s": .3},
    "corner": "decor",
    "res": "1080",  # 画质：1080 / 1440（2K）/ 2160（4K），指竖屏的短边；直播窗口按这个物理像素开，OBS 浏览器源也填这个尺寸
    "persona": "",  # 唠嗑直播当前的人设 id（data/personas.json）
    "cold": {"on": True, "gap": 45},
    "charfit": "auto",  # 掌柜显示方式：auto（小动物全身、人物半身、跳舞全身）/ half / full；各场景可以另设（scenes[场景].fit）
    "scenes": {},  # 各场景自己的组件开关和位置 {场景: {show: {元素: 开关}, layout: {元素: 位置}}}；主场景用 show / layout
    "activity": {},  # 唠嗑活动的设置（跳舞几首、游戏类型、礼物心愿），默认值在 engine.Activity.DEFAULT  # 唠嗑冷场：多少秒没人说话，主播就从人设的话题库里挑一个主动开聊
    "show_auto": {"carousel": False, "dwell": 12, "link": True, "director": True, "lock": False},  # 店铺展示场景：自动轮播 / 精选项目停几秒 / 关键词联动 / AI 导播 / 锁定画面
    "looks": {},  # 店铺直播 / 唠嗑各自上次用的背景和主题（mode_look 维护）
    "mode": "shop",  # 直播类型：shop = 店铺直播（掌柜按店铺资料回答）；chat = 唠嗑（纯陪聊，不讲店铺）  # 右下角那块：场景自带的装饰（扭蛋机等）/ 音乐播放器 / 空着
    "music": {"vol": .35, "duck": .25, "mode": "list"},  # 背景音乐：音量、掌柜说话时压到多少、播放方式（由 /api/music 改）
    # 弹幕接收：直播间地址、进场欢迎（间隔秒）、留言最多等多久（秒）、屏蔽词
    "danmaku": {"url": "", "song": True, "song_cut": True, "song_online": True, "thanks": True, "welcome": True, "welcome_mode": "merge", "welcome_text": "欢迎{names}来到直播间～", "welcome_gap": 60, "max_wait": 90, "batch_at": 2, "batch_max": 4, "block": []},  # Live2D 衣服上的店铺标志：开关、位置（相对上衣网格）、宽度比例
    "layout": {},  # 小店场景里各元素的位置大小（百分比），在直播控制台「编辑布局」里拖出来的；空 = 默认布局
    "grid": 2.5,   # 编辑布局时的网格吸附间距（占画面的百分比），0 = 不吸附
    "theme": {"preset": "neon"},  # 小店场景主题：预设名 + 可选的自定义颜色 / 字体覆盖（card alpha ink acc acc2 font）
    "styles": {},  # 单个元素的样式覆盖 {元素: {ink, card, alpha, fs, font}}
    "texts": {},  # 内置元素的文字（黑板、营业牌、公告标签、名牌……），键见 LIVE_TEXTS；留空 = 默认
    "order": [],  # 图层顺序：元素键从下到上；空 = 默认层次
    "widgets": [],  # 自己加的小组件 [{id, type, on, props}]，类型见 WIDGET_TYPES
    "tests": ["你好呀", "周末门票多少钱？", "几点关门？", "三岁小孩能玩什么？", "可以办生日会吗？", "有游泳池吗？"],  # 模拟留言的快捷预设
    "phrases": ["欢迎刚进直播间的朋友，有什么想问的直接发弹幕问我哦！", "点左下角的团购链接，就能抢直播间专享价啦～",
                "想带孩子来玩的，可以先看看我们的营业时间和门票价格，有问题随时问我。", "喜欢的话点个关注，下次开播第一时间提醒你！"],
}


SCENE_PARTS = ("char", "namecard", "clock", "ticker", "chalk", "bubble", "open", "log", "counter", "ailabel", "music", "wish", "game")  # 小店场景里能拖动的元素
WIDGET_TYPES = ("text", "image", "countdown", "progress", "prices", "tokens")  # …… / token 账本（今天花了多少、礼物赚了多少）  # 文字标签 / 图片（二维码）/ 倒计时 / 目标进度 / 价目表
MOTIONS = ("nod", "shake", "tilt", "happy", "wink", "think", "surprise", "look", "sway", "bow", "shy", "cheer")  # 和 ui.html 的 MOTIONS 对应
LIVE_TEXTS = ("icon", "icon_img", "title", "slogan", "eyebrow", "tag", "notice", "chalk", "open_mode", "open_on", "open_off", "log_title", "who", "ailabel")
LIVE_IMG_DIR = ROOT / "data" / "live_img"  # 图片组件上传的图


def _part_key(k) -> bool:
    return k in SCENE_PARTS or bool(re.fullmatch(r"w_[0-9a-z]{4,12}", str(k)))


VOICE_KEYS = ("engine", "voice", "rate", "pitch")
LOOK_KEYS = ("bg", "theme", "texts", "layout", "widgets", "styles", "show", "order", "corner", "badge", "scenes", "charfit")  # 两种直播各存一份的画面设置
LOOK_REPLACE = tuple(("live", k) for k in ("theme", "texts", "layout", "widgets", "styles", "order", "looks", "scenes"))


def mode_look(engine, patch: dict) -> tuple[dict, dict | None]:
    """店铺直播和唠嗑完全分开：各记各的背景、主题、文字、组件和位置、形象和音色。换直播类型时把当前这套存起来，
    返回 (补丁 + 另一边上次的背景主题, 另一边上次的整套)；另一边还没用过时整套是 None。补丁里自带的优先。"""
    live, new = engine.cfg["live"], patch.get("mode")
    old = live.get("mode", "shop")
    if not new or new == old:
        return patch, None
    kind = engine.kind
    cur = {**{k: live[k] for k in LOOK_KEYS if k in live}, "voice": {k: engine.cfg["tts"].get(k) for k in VOICE_KEYS},
           "avatar": {"kind": kind, "card": engine.cfg.get("avatar_kind") or kind,
                      "paths": {k: v for k, v in engine.cfg["avatar"].items() if k in CHARACTER_KINDS}}}
    looks = {**live.get("looks", {}), old: cur}
    back = looks.get(new)
    base = {k: back[k] for k in LOOK_KEYS if k in back} if back else {"bg": "@chat:bedroom" if new == "chat" else "", "texts": {}, "widgets": []}
    return {**{k: v for k, v in base.items() if k not in patch}, **patch, "looks": looks}, back


def apply_look(engine, voice: dict | None, avatar: dict | None):
    """套上一套音色和形象（人设「使用」、切换直播类型时换回上次那套）。"""
    from engine import KINDS
    if voice:
        tts = {**engine.cfg["tts"], **{k: v for k, v in voice.items() if k in VOICE_KEYS and v}}
        tts = {k: v for k, v in tts.items() if not k.startswith("api_key")}
        save_settings({"tts": tts})
        engine.apply_settings({"tts": tts})
    if not avatar:
        return
    kind = avatar.get("kind") or engine.kind
    paths = {**avatar.get("paths", {}), **({kind: avatar["path"]} if avatar.get("path") else {})}  # 人设给一条 path；切换直播类型换回整套
    if paths:
        save_settings({"avatar": paths})
        engine.cfg["avatar"].update(paths)
    if kind in KINDS:
        engine.set_kind(kind, avatar.get("card") or kind)


def clean_live_patch(body: dict) -> dict:
    """直播设置补丁只收认识的键，并逐项校验类型和长度（这些值会写进直播画面的样式里）。"""
    s = lambda v, n=300: str(v)[:n]
    num = lambda v, lo, hi: max(lo, min(hi, float(v)))
    p = {k: v for k, v in body.items() if k in LIVE_DEFAULTS and k not in ("port", "token")}
    if "show" in p:
        p["show"] = {k: bool(v) for k, v in p["show"].items() if k in LIVE_DEFAULTS["show"]}
    if "layout" in p:  # 每个元素 {x, y, w, h}，都是占画面的百分比
        p["layout"] = {k: {d: num(v[d], -100, 200) for d in ("x", "y", "w", "h") if d in v}
                       for k, v in p["layout"].items() if _part_key(k) and isinstance(v, dict)}
    if "scenes" in p:  # 舞台跳舞 / 小游戏（唠嗑），项目墙 / 项目详情（店铺）：各自的组件开关和位置
        sc = p["scenes"] if isinstance(p["scenes"], dict) else {}
        p["scenes"] = {sid: {"show": {k: bool(v) for k, v in (x.get("show") or {}).items() if _part_key(k)},
                             "order": list(dict.fromkeys(str(k) for k in (x.get("order") or []) if _part_key(k)))[:60],
                             "fit": x.get("fit") if x.get("fit") in ("half", "full") else "",
                             "layout": {k: {d: num(r[d], -100, 200) for d in ("x", "y", "w", "h") if d in r}
                                        for k, r in (x.get("layout") or {}).items() if _part_key(k) and isinstance(r, dict)}}
                       for sid, x in sc.items() if sid in ("dance", "game", "wall", "detail") and isinstance(x, dict)}
    if "charfit" in p and p["charfit"] not in ("auto", "half", "full"):
        p.pop("charfit")
    if "grid" in p:
        p["grid"] = num(p["grid"], 0, 10)
    if "theme" in p:
        t = p["theme"]
        p["theme"] = {k: (num(t[k], 0, 100) if k == "alpha" else s(t[k], 40)) for k in ("preset", "card", "alpha", "ink", "acc", "acc2", "font") if k in t}
    if "styles" in p:
        p["styles"] = {k: {a: (num(v[a], 0, 100) if a == "alpha" else num(v[a], .4, 3) if a == "fs" else s(v[a], 40))
                           for a in ("ink", "card", "alpha", "fs", "font") if a in v}
                       for k, v in p["styles"].items() if _part_key(k) and isinstance(v, dict)}
    if "widgets" in p:
        p["widgets"] = [{"id": w["id"], "type": w["type"], "on": bool(w.get("on", True)),
                         "props": {a: s(b, 2000) for a, b in (w.get("props") or {}).items() if re.fullmatch(r"[a-z]{1,12}", a)}}
                        for w in p["widgets"][:30] if _part_key(w.get("id")) and w.get("type") in WIDGET_TYPES]
    if "badge" in p:
        b = p["badge"]
        p["badge"] = {**{k: (bool(b[k]) if k == "on" else num(b[k], 0, 1)) for k in ("on", "x", "y", "s") if k in b}}
    if "corner" in p and p["corner"] not in ("decor", "music", "none"):
        p.pop("corner")
    if "res" in p and str(p["res"]) not in ("1080", "1440", "2160"):
        p.pop("res")
    if "mode" in p and p["mode"] not in ("shop", "chat"):
        p.pop("mode")
    if "persona" in p:
        p["persona"] = re.sub(r"[^0-9a-zA-Z]", "", str(p["persona"]))[:16]
    if "cold" in p:
        c = p["cold"]
        p["cold"] = {**({"on": bool(c["on"])} if "on" in c else {}), **({"gap": num(c["gap"], 15, 600)} if "gap" in c else {})}
    p.pop("music", None)  # 音乐只走 /api/music
    if "activity" in p:
        a = p["activity"] if isinstance(p["activity"], dict) else {}
        d, g, w = a.get("dance") or {}, a.get("game") or {}, a.get("wish") or {}
        p["activity"] = {"dance": {"songs": int(num(d.get("songs", 1), 0, 10)), "power": num(d.get("power", 1), .3, 1.6)},
                         "game": {"type": g.get("type") if g.get("type") in ("guess", "quiz") else "guess", "rounds": int(num(g.get("rounds", 5), 1, 20)),
                                  "secs": int(num(g.get("secs", 20), 8, 60)), "topic": s(g.get("topic", ""), 60), "level": g.get("level") if g.get("level") in ("easy", "mid", "hard") else "mid"},
                         "wish": {"on": bool(w.get("on")), "items": [{"gift": s(i.get("gift", ""), 12).strip(), "n": int(num(i.get("n", 1), 1, 99999)),
                                                                       "reward": s(i.get("reward", ""), 30), "act": i.get("act") if i.get("act") in ("dance", "game") else ""}
                                                                      for i in (w.get("items") or [])[:6] if isinstance(i, dict) and str(i.get("gift", "")).strip()]}}
    if "show_auto" in p:
        a = p["show_auto"]
        p["show_auto"] = {**{k: bool(a[k]) for k in ("carousel", "link", "director", "lock") if k in a}, **({"dwell": num(a["dwell"], 5, 120)} if "dwell" in a else {})}
    p.pop("looks", None)  # 两种直播各自的背景 / 主题只由 mode_look 写
    if "danmaku" in p:
        d = p["danmaku"]
        p["danmaku"] = {**({"url": s(d["url"], 300)} if "url" in d else {}), **({"welcome": bool(d["welcome"])} if "welcome" in d else {}),
                        **({"thanks": bool(d["thanks"])} if "thanks" in d else {}),
                        **{k: bool(d[k]) for k in ("song", "song_cut", "song_online") if k in d},
                        **({"welcome_mode": d["welcome_mode"]} if d.get("welcome_mode") in ("merge", "instant") else {}),
                        **({"welcome_text": s(d["welcome_text"], 120)} if "welcome_text" in d else {}),
                        **{k: num(d[k], 10, 600) for k in ("welcome_gap", "max_wait") if k in d},
                        **({"batch_at": 0 if int(num(d["batch_at"], 0, 10)) < 2 else int(num(d["batch_at"], 0, 10))} if "batch_at" in d else {}),
                        **({"batch_max": int(num(d["batch_max"], 2, 8))} if "batch_max" in d else {}),
                        **({"block": [s(w, 20) for w in d["block"] if str(w).strip()][:100]} if "block" in d else {})}
    if "motion" in p:
        p["motion"] = {k: bool(v) for k, v in p["motion"].items() if k in LIVE_DEFAULTS["motion"]}
    if "texts" in p:
        p["texts"] = {k: s(v, 600) for k, v in p["texts"].items() if k in LIVE_TEXTS}
    if "order" in p:
        p["order"] = list(dict.fromkeys(str(k) for k in p["order"] if _part_key(k)))[:60]
    for k in ("phrases", "tests"):
        if k in p:
            p[k] = [s(x) for x in p[k] if str(x).strip()][:50]
    return p


def load_config() -> dict:
    import yaml

    for p in (ROOT / "config.yaml", ROOT / "app" / "config.yaml"):
        if p.exists():
            text = p.read_text(encoding="utf-8")
            cfg = yaml.safe_load(text)
            plain = cfg["llm"].get("api_key")
            if plain:  # 旧版 config.yaml 里的明文 Key：加密迁移进 settings.json，再把原文件里的明文清掉
                save_settings({"llm": {"api_key": plain}})
                p.write_text(re.sub(r'(?m)^(\s*api_key:\s*)"?[^"#\n]*"?', r'\1""', text, count=1), encoding="utf-8")
                print("[安全] 已把 config.yaml 里的明文 API Key 加密迁移，并清除明文", flush=True)
            break
    else:
        raise FileNotFoundError("找不到 config.yaml")
    if SETTINGS.exists():
        saved = read_json(SETTINGS, {})
        if saved.get("llm", {}).get("api_key"):  # 旧版 settings.json 里的明文 Key：就地加密
            save_settings({"llm": {"api_key": saved["llm"]["api_key"]}})
            saved = read_json(SETTINGS, {})
            print("[安全] 已把 settings.json 里的明文 API Key 加密", flush=True)
        _merge(cfg, saved)
    # 直播：固定端口 + 长期有效的只读令牌（OBS 里填一次直播画面地址，重启也不用改）+ 画面、接管、话术的默认值
    live = cfg.setdefault("live", {})
    for k, v in LIVE_DEFAULTS.items():
        live.setdefault(k, v)
    for k, v in LIVE_DEFAULTS["show"].items():
        live["show"].setdefault(k, v)
    if not live.get("token"):
        live["token"] = secrets.token_urlsafe(18)
        save_settings({"live": {"token": live["token"]}})
    for group in KEY_GROUPS:
        g = cfg.setdefault(group, {})
        g["api_key"] = ""
        if g.get("api_key_enc"):
            try:
                g["api_key"] = unseal_key(g)  # 明文只在内存里
            except OSError as e:
                print(f"[安全] {e}", flush=True)
    return cfg


KEY_GROUPS = ("llm", "tts")  # 设置里带 API Key 的分组：大模型、云端语音（通义千问）


def save_settings(patch: dict, replace: tuple = ()):
    """写 data/settings.json；API Key 只以 DPAPI 密文 + SHA-256 落盘，绝不写明文。
    默认按层合并；replace 里的 (分组, 键) 整个替换（比如布局恢复默认 = 清空，合并就清不掉）。"""
    patch = json.loads(json.dumps(patch))
    for group in KEY_GROUPS:
        g = patch.get(group, {})
        if g.get("api_key"):
            g.update(seal_key(g.pop("api_key")))
        g.pop("api_key", None)
    with SETTINGS_LOCK:  # 读 → 合并 → 写 必须一气呵成：两个请求同时改设置时，不然会互相覆盖，甚至把文件写坏
        cur = read_json(SETTINGS, {})
        for group in KEY_GROUPS:
            cur.get(group, {}).pop("api_key", None)
        out = _merge(cur, patch)
        for group, key in replace:
            if key in patch.get(group, {}):
                out[group][key] = patch[group][key]
        save_text(SETTINGS, json.dumps(out, ensure_ascii=False, indent=2))


# Live2D / VRM / Rive 角色文件只从这两处找（动物放在其下的 animal\ 子目录），/files/ 也只放行这些子目录
CHARACTER_DIRS = ("data/samples", "data/avatars")
CHARACTER_KINDS = {"live2d": "*/*.model3.json", "vrm": "*.vrm", "rive": "*.riv"}


def characters() -> dict:
    """可选角色：{"live2d": {相对路径: {"name": 名字, "animal": 是否动物}}, "vrm": {...}, "rive": {...}}。"""
    found = {k: {} for k in CHARACTER_KINDS}
    for d in CHARACTER_DIRS:
        for animal in (False, True):
            for kind, pattern in CHARACTER_KINDS.items():
                for p in sorted((ROOT / d / ("animal" if animal else "") / kind).glob(pattern)):
                    name = p.parent.name if kind == "live2d" else p.stem
                    lic = p.parent / "许可与署名.txt"  # 下载 / 导入的模型：许可和署名要求（唠嗑页显示）
                    note = lic.read_text(encoding="utf-8") if lic.exists() else \
                        "Live2D 官方免费素材：个人或年营收低于 1000 万日元的小企业可商用，中大型企业不可用于商业" if d == "data/samples" and kind == "live2d" else ""
                    found[kind][p.relative_to(ROOT).as_posix()] = {"name": name, "animal": animal, "note": note}
    return found


def character_file(rel: str) -> tuple[Path, str] | None:
    """/files/ 的路径 → (根目录, 相对路径)；只放行 CHARACTER_DIRS 下的 [animal/]live2d|vrm|rive。"""
    parts = rel.split("/")
    sub = parts[3] if len(parts) > 3 and parts[2] == "animal" else parts[2] if len(parts) > 2 else ""
    if "/".join(parts[:2]) in CHARACTER_DIRS and sub in CHARACTER_KINDS:
        return ROOT / parts[0] / parts[1], "/".join(parts[2:])
    return None


def settings_view(engine) -> dict:
    """给设置面板的当前值；API Key 不回传明文。"""
    from engine import KINDS, MODES, QWEN_VOICES, TTS_ENGINES, VOICES, LocalTTS, SovitsTTS, ref_voices

    c = engine.cfg
    chars = characters()
    for k in CHARACTER_KINDS:  # 没选过角色就用找到的第一个（人物优先）
        if c["avatar"].get(k) not in chars[k] and chars[k]:
            c["avatar"][k] = min(chars[k], key=lambda p: chars[k][p]["animal"])
    if c["llm"].get("provider") not in PROVIDERS:  # 没记录服务商时按接口地址识别
        c["llm"]["provider"] = next((k for k, p in PROVIDERS.items() if p[1] and p[1] == c["llm"]["base_url"]), "custom")
    tts = {k: v for k, v in c["tts"].items() if not k.startswith("api_key")}
    tts.update(key_set=bool(c["tts"].get("api_key")), key_fp=(c["tts"].get("api_key_sha256") or "")[:8])
    return {"name": c.get("name", "悠悠"), "persona": c["persona"], "tts": tts, "vad": c["vad"], "mode": engine.mode,
            "av": {"offset_ms": c.get("av", {}).get("offset_ms", 0), "out_latency_ms": round(engine.player.out_latency * 1000)},
            "llm": {"provider": c["llm"].get("provider", "custom"), "base_url": c["llm"]["base_url"], "model": c["llm"]["model"],
                    "key_set": bool(c["llm"].get("api_key")), "key_fp": (c["llm"].get("api_key_sha256") or "")[:8]},
            "providers": {k: {"name": n, "base_url": u, "models": m} for k, (n, u, m) in PROVIDERS.items()},
            "voices": VOICES, "qwen_voices": QWEN_VOICES, "ref_voices": ref_voices(MODELS), "tts_engines": TTS_ENGINES,
            "local_ok": {"qwen_local": (MODELS / "qwen3-tts-0.6b").exists(), "sovits": bool(SovitsTTS.python(ROOT)),
                         "design": (MODELS / "qwen3-tts-1.7b-design").exists()}, "local_voices": LocalTTS.names(MODELS), "modes": {k: {"name": v[2], "desc": v[3], "hd": v[1]} for k, v in MODES.items()},
            "avatar": c["avatar"], "kind": engine.kind, "last_kind": c.get("avatar_kind"), "kinds": KINDS, "characters": chars}


def live_view(engine) -> dict:
    """直播控制台要的全部设置（含背景库列表、声音设备）。"""
    from engine import AudioOut

    c = engine.cfg["live"]
    bgs = sorted((p.name for p in LIVE_BG_DIR.glob("*.jpg")), reverse=True) if LIVE_BG_DIR.exists() else []
    sections = [{"id": x["id"], "title": x["title"]} for x in engine.shop.get("sections", [])]  # 价目表组件可选的分类
    return {"url": engine.live_url, "devices": AudioOut.devices(), "bgs": bgs, "sections": sections,
            **{k: c[k] for k in ("device", "monitor", "scene", "ratio", "bgmode", "cap", "bg", "show", "auto", "phrases", "follow",
                                 "layout", "grid", "theme", "styles", "widgets", "tests", "texts", "order", "motion", "badge", "danmaku", "corner", "res", "mode", "persona", "cold", "show_auto", "activity", "scenes", "charfit")},
            "showcase": engine.showcase, "dm": engine.danmaku.view(), "mu": engine.music.view()}


def live_state(engine) -> dict:
    """直播画面要的东西：当前形象、店铺展示信息（不含给大模型的知识库）、画面设置、最近几条对话。"""
    shop = {k: engine.shop.get(k, "") for k in (*SHOP_TEXT, "logo")}
    cats = engine.show.cats()  # 按店铺里排好的分类顺序
    shop["projects"] = sorted((p for p in engine.shop.get("projects", []) if p.get("enabled", True)), key=lambda p: cats.index(p.get("cat") or "其他"))
    live = {k: engine.cfg["live"][k] for k in ("scene", "ratio", "bgmode", "cap", "bg", "show", "follow", "layout", "theme", "styles", "widgets", "texts", "order", "motion", "badge", "corner", "mode", "scenes", "charfit")}
    secs = {x["id"]: x for x in engine.shop.get("sections", [])}
    wdata = {w["id"]: [{"title": i["title"], "text": i["text"]} for i in secs[w["props"]["section"]]["items"] if i.get("enabled", True)]
             for w in live["widgets"] if w["type"] == "prices" and w["props"].get("section") in secs}  # 价目表：取店铺知识库里那个分类的条目
    st = {**engine.avatar_state(), "showcase": engine.showcase, "picks": engine.show.picks, "shop": shop, "live": live, "wdata": wdata, "ledger": engine.ledger.view(), "recent": engine.history[-8:], "music": engine.music.view(),
          "activity": engine.act.view(), "beats": engine.music.beats}
    if live["mode"] == "chat" and engine.persona.get("name"):
        st["name"] = engine.persona["name"]
    return st


def save_image(raw: bytes, dst: Path, square: bool, keep_alpha=False):
    """logo：缩到 1024、PNG 保留透明；背景：长边不超过 1920、JPG；keep_alpha：长边 1200、PNG（二维码、贴纸）。按 EXIF 摆正手机照片。"""
    import io

    from PIL import Image, ImageOps

    dst.parent.mkdir(parents=True, exist_ok=True)
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    if keep_alpha:
        img = img.convert("RGBA")
        img.thumbnail((1200, 1200))
        img.save(dst)
    elif square:
        img = img.convert("RGBA")
        img.thumbnail((1024, 1024))
        img.save(dst)
    else:
        img = img.convert("RGB")
        img.thumbnail((1920, 1920))
        img.save(dst, quality=90)


def prune_media(shops: dict):
    used = {m["f"] for s in shops["shops"] for p in s.get("projects", []) for m in p.get("media", [])}
    for f in MEDIA_DIR.glob("*") if MEDIA_DIR.exists() else []:
        if f.name not in used and time.time() - f.stat().st_mtime > 3600:  # 刚传还没保存的先留着
            f.unlink(missing_ok=True)


def shop_action(engine, shops: dict, body: dict) -> dict:
    """店铺档案的管理操作：保存 / 新建 / 复制 / 删除 / 设为使用中 / 导入 / 导出。返回最新的全部档案。"""
    import copy
    import time

    act, by_id = body.get("action"), {s["id"]: s for s in shops["shops"]}
    if act in ("save", "delete"):
        threading.Timer(1, prune_media, args=(shops,)).start()  # 保存完再清，没被引用的图片 / 视频删掉
    if act == "save":
        shop = clean_shop(body["shop"])
        shops["shops"] = [shop if s["id"] == shop["id"] else s for s in shops["shops"]]
        if shop["id"] not in by_id:
            shops["shops"].append(shop)
    elif act in ("create", "duplicate", "import"):
        if act == "create":
            shop = make_shop()
        else:
            src = by_id[body["id"]] if act == "duplicate" else body["shop"]
            shop = clean_shop({**copy.deepcopy(src), "id": _uid(), "logo": ""})
            shop["name"] = (shop["name"] or "店铺") + ("（副本）" if act == "duplicate" else "")
        shops["shops"].append(shop)
        shops["new"] = shop["id"]
    elif act == "delete":
        if len(shops["shops"]) <= 1:
            raise ValueError("至少要保留一家店铺")
        shops["shops"] = [s for s in shops["shops"] if s["id"] != body["id"]]
        (SHOP_DIR / f"{body['id']}.png").unlink(missing_ok=True)
        if shops["active"] == body["id"]:
            shops["active"] = shops["shops"][0]["id"]
    elif act == "activate":
        shops["active"] = by_id[body["id"]]["id"]
    elif act == "export":
        safe = re.sub(r'[\\/:*?"<>|]', "_", by_id[body["id"]]["name"])  # 文件名里不能有的字符
        out = ROOT / "data" / "exports" / f"店铺_{safe}_{time.strftime('%Y%m%d-%H%M%S')}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(by_id[body["id"]], ensure_ascii=False, indent=1), encoding="utf-8")
        return {**shops, "exported": str(out)}
    else:
        raise ValueError("未知操作")
    new = shops.pop("new", None)
    save_shops(shops)
    engine.set_shop(next(s for s in shops["shops"] if s["id"] == shops["active"]))  # 使用中的店铺可能变了
    engine.show.check()  # 正在展示的项目可能被删了 / 关了
    return {**shops, **({"new": new} if new else {})}


def shop_ask(engine, shop: dict, question: str) -> dict:
    """试问：用（还没保存的）店铺资料回答一个问题，只出文字，不说话、不进对话记录。"""
    import time

    from engine import LLM, build_system

    t = time.perf_counter()
    llm = LLM(engine.cfg["llm"], build_system(engine.cfg, clean_shop(shop)))
    return {"answer": "".join(llm.sentences(question)), "ms": round((time.perf_counter() - t) * 1000)}


def shop_extract(engine, text: str) -> dict:
    """从文本整理：把一段店铺介绍交给大模型，拆成「分类 → 条目」，由界面追加到知识库里（商家再核对）。"""
    from openai import OpenAI

    c = engine.cfg["llm"]
    prompt = ("把下面的店铺介绍整理成知识库，只输出 JSON，格式：{\"sections\": [{\"title\": \"分类名\", \"items\": [{\"title\": \"条目名\", \"text\": \"内容\"}]}]}。"
              "分类用「基本信息、项目、价格、规则须知、常见问答、活动」这类名字；每个条目一件事；原文的数字、价格、时间一字不改，原文没有的不要补。\n\n" + text[:20000])
    r = OpenAI(api_key=c.get("api_key") or "local", base_url=c["base_url"], timeout=120).chat.completions.create(
        model=c["model"], messages=[{"role": "user", "content": prompt}], temperature=0.1)
    m = re.search(r"\{.*\}", r.choices[0].message.content, re.S)
    sections = json.loads(m.group(0)).get("sections", []) if m else []
    return {"sections": clean_shop({"sections": sections})["sections"]}


def llm_test(base_url: str, model: str, key: str) -> dict:
    """测试连接：发一句很短的请求，返回首字耗时和回复。"""
    import time
    from openai import OpenAI

    check_base_url(base_url)
    t = time.perf_counter()
    r = OpenAI(api_key=key or "local", base_url=base_url, timeout=20).chat.completions.create(
        model=model, messages=[{"role": "user", "content": "用一句话打个招呼"}], max_tokens=40)
    return {"ok": True, "ms": round((time.perf_counter() - t) * 1000), "reply": r.choices[0].message.content.strip()}


def llm_models(base_url: str, key: str) -> dict:
    """从服务商拉取可用模型列表（OpenAI 兼容的 GET /models）。"""
    from openai import OpenAI

    check_base_url(base_url)
    return {"ok": True, "models": sorted(m.id for m in OpenAI(api_key=key or "local", base_url=base_url, timeout=20).models.list())}


def make_handler(engine, token: str, transport: Transport, shops: dict, personas: dict):
    from engine import EV

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _authorized(self, live_ok=False) -> bool:
            """只接受本程序窗口的请求：Host 必须是本机（防 DNS 重绑定），且带本次启动的随机令牌（防网页/其他程序冒用）。
            live_ok：直播画面页（给 OBS 用）只读的那几个地址，也接受长期有效的直播令牌（k 参数或 lt Cookie）。"""
            from urllib.parse import parse_qs, urlparse

            from http.cookies import SimpleCookie

            host = (self.headers.get("Host") or "").split(":")[0]
            cookies, query = SimpleCookie(self.headers.get("Cookie") or ""), parse_qs(urlparse(self.path).query)
            got = (self.headers.get("X-Token") or query.get("t", [""])[0]
                   or (cookies["t"].value if "t" in cookies else ""))  # Live2D / three.js 加载贴图等文件时带不了请求头，靠 Cookie
            live = query.get("k", [""])[0] or (cookies["lt"].value if "lt" in cookies else "")
            if host in ("127.0.0.1", "localhost") and (secrets.compare_digest(got, token) or
                                                      (live_ok and secrets.compare_digest(live, engine.cfg["live"]["token"]))):
                return True
            self._send(403, "未授权：请从AI虚拟主播的程序窗口访问".encode(), "text/plain; charset=utf-8")
            return False

        def _send(self, code, body: bytes, ctype, headers=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            self._send(200, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            from urllib.parse import unquote

            path = self.path.split("?")[0]
            # 直播画面页只读用到的地址：页面、事件流、真人画面、当前形象、前端库和角色文件
            live_ok = path in ("/live", "/events", "/video", "/api/live_state", "/api/activity_state") or path.startswith(("/lib/", "/files/", "/shop/", "/livebg/", "/liveimg/", "/scene/"))
            if not self._authorized(live_ok):
                return
            try:
                if path == "/api/pubkey":  # 端到端加密用的临时公钥
                    self._json({"spki": transport.public_spki()})
                elif path == "/":
                    self._send(200, (BUNDLE / "ui.html").read_bytes(), "text/html; charset=utf-8",
                               [("Set-Cookie", f"t={token}; Path=/; HttpOnly; SameSite=Strict")])
                elif path == "/live":  # 直播画面：同一个页面的“直播模式”，只有数字人 + AI 标识 + 字幕
                    self._send(200, (BUNDLE / "ui.html").read_bytes(), "text/html; charset=utf-8",
                               [("Set-Cookie", f"lt={engine.cfg['live']['token']}; Path=/; HttpOnly; SameSite=Strict")])
                elif path == "/api/live_state":
                    self._json(live_state(engine))
                elif path == "/api/activity_state":  # 直播画面定时核对活动状态（事件漏收也能追上）
                    self._json(engine.act.view())
                elif path.startswith("/shop/media/"):  # 游玩项目的图片 / 视频
                    self._file(MEDIA_DIR, unquote(path[12:]))
                elif path.startswith("/shop/"):  # 店铺 logo
                    self._file(SHOP_DIR, unquote(path[6:]))
                elif path.startswith("/livebg/"):  # 直播背景库
                    self._file(LIVE_BG_DIR, unquote(path[8:]))
                elif path.startswith("/liveimg/"):  # 图片组件
                    self._file(LIVE_IMG_DIR, unquote(path[9:]))
                elif path == "/api/shops":
                    self._json(shops)
                elif path == "/api/live":
                    self._json(live_view(engine))
                elif path == "/api/showcase":  # 导播记录 + 当前场景 + 组合
                    self._json(engine.show.view())
                elif path.startswith("/scene/"):  # 内置场景的底图（app/scene_*.webp）
                    self._file(BUNDLE, "scene_" + Path(unquote(path[7:])).name)
                elif path.startswith("/lib/"):  # 前端库：Live2D / three.js / three-vrm
                    if path[5:] == CUBISM_CORE:
                        fetch_cubism_core()
                    self._file(ROOT / "third_party" / "web", path[5:])
                elif path.startswith("/files/"):  # 角色模型（见 character_file）
                    hit = character_file(unquote(path[7:]))
                    self._file(*hit) if hit else self._send(404, b"", "text/plain")
                elif path == "/video":
                    self._video()
                elif path == "/events":
                    self._events()
                elif path == "/api/settings":
                    self._json(settings_view(engine))
                elif path == "/api/personas":  # 唠嗑：人设列表 + 当前人设 + 内置场景
                    self._json({**personas, "active": engine.cfg["live"].get("persona", ""), "scenes": CHAT_SCENES})
                elif path in ("/api/corpus", "/api/corpus_item"):  # 语料库：按条件分页列出 / 单条的完整上下文
                    from urllib.parse import parse_qs

                    qs = {k: v[0] for k, v in parse_qs(self.path.partition("?")[2]).items()}
                    if path == "/api/corpus_item":
                        self._json(engine.corpus.item(int(qs.get("id", 0))))
                    else:
                        self._json(engine.corpus.query(qs, int(qs.get("page", 0) or 0)))
                elif path == "/api/danmaku":  # 弹幕接收状态 + 最近的弹幕和处理结果（控制台轮询）
                    self._json(engine.danmaku.view())
                elif path == "/api/history":
                    self._json(engine.history)
                elif path == "/avatar/image":  # 当前形象照片（设置面板缩略图）
                    import cv2

                    img = cv2.imread(str(ROOT / engine.cfg["avatar"]["image"]))
                    img = cv2.resize(img, (320, int(img.shape[0] * 320 / img.shape[1])))
                    self._send(200, cv2.imencode(".jpg", img)[1].tobytes(), "image/jpeg")
                else:
                    self._send(404, b"", "text/plain")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def _file(self, base: Path, rel: str):
            """发 base 目录下的静态文件；解析后必须仍在 base 里（防 ../ 越界）。"""
            p = (base / rel).resolve()
            if not p.is_file() or not p.is_relative_to(base.resolve()):
                return self._send(404, b"", "text/plain")
            types = {".js": "text/javascript", ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp",
                     ".wasm": "application/wasm", ".mp4": "video/mp4", ".webm": "video/webm"}
            self._send(200, p.read_bytes(), types.get(p.suffix.lower(), "application/octet-stream"),
                       [("Cache-Control", "max-age=3600")])

        def _video(self):  # MJPEG：<img src="/video"> 直接显示
            p = engine.player
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            seq = -1
            while True:
                with p.cond:
                    p.cond.wait_for(lambda: p.seq != seq, timeout=1)
                    jpg, seq = p.jpeg, p.seq
                if jpg:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg) + jpg + b"\r\n")

        def _events(self):  # SSE：日志 / 对话 / 流程耗时 / 系统信息
            q, history = EV.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for e in history:
                    self.wfile.write(f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        e = q.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            finally:
                EV.unsubscribe(q)

        def do_POST(self):
            from urllib.parse import parse_qs, unquote, urlparse

            url = urlparse(self.path)
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if not self._authorized():
                return
            try:
                if url.path == "/api/avatar":  # 原始文件内容上传：照片或视频
                    name = Path(unquote(parse_qs(url.query).get("name", ["upload.jpg"])[0])).name
                    # 只保留扩展名：Windows 上 OpenCV 打不开含中文的路径（imread 返回 None）
                    dst = ROOT / "data" / "avatars" / "uploads" / f"{int(__import__('time').time())}{Path(name).suffix.lower()}"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(raw)
                    rel = engine.change_avatar(dst)
                    save_settings({"avatar": rel})
                    return self._json({"ok": True, **rel})
                if url.path == "/api/shop_logo":  # 某家店的 logo：原始图片内容上传，空内容 = 删除
                    shop = next(s for s in shops["shops"] if s["id"] == parse_qs(url.query).get("id", [""])[0])
                    if raw:
                        save_image(raw, SHOP_DIR / f"{shop['id']}.png", square=True)
                    else:
                        (SHOP_DIR / f"{shop['id']}.png").unlink(missing_ok=True)
                    shop["logo"] = f"{shop['id']}.png?v={int(__import__('time').time())}" if raw else ""
                    return self._json(shop_action(engine, shops, {"action": "save", "shop": shop}))
                if url.path == "/api/shop_media":  # 游玩项目的图片 / 视频：存下来返回文件名，由控制台写进项目（点「保存」生效）
                    sid = re.sub(r"[^0-9a-zA-Z]", "", parse_qs(url.query).get("shop", [""])[0])[:16]
                    ext = Path(unquote(parse_qs(url.query).get("name", ["x.jpg"])[0])).suffix.lower()
                    if not sid or not raw:
                        return self._json({"error": "没有文件"})
                    if ext in (".mp4", ".webm"):
                        if len(raw) > 300 * 1024 * 1024:
                            return self._json({"error": "视频太大（最多 300MB），请先压缩"})
                        name = f"{sid}_{_uid()}{ext}"
                        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
                        (MEDIA_DIR / name).write_bytes(raw)
                        return self._json({"f": name, "t": "video"})
                    name = f"{sid}_{_uid()}.jpg"
                    save_image(raw, MEDIA_DIR / name, square=False)
                    return self._json({"f": name, "t": "img"})
                if url.path == "/api/live_img":  # 图片组件（二维码、海报、贴纸）：保存后返回文件名，由控制台写进组件
                    name = f"{int(__import__('time').time() * 1000)}.png"
                    save_image(raw, LIVE_IMG_DIR / name, square=False, keep_alpha=True)
                    return self._json({"name": name})
                if url.path == "/api/import_live2d":  # 导入 Live2D 模型（zip）
                    q = parse_qs(url.query)
                    return self._json(import_live2d(raw, unquote(q.get("name", ["model"])[0]), q.get("animal", ["0"])[0] == "1"))
                if url.path == "/api/music_upload":  # 背景音乐：原始文件上传到 data/music（文件名保留，方便认）
                    name = Path(unquote(parse_qs(url.query).get("name", ["music.mp3"])[0])).name
                    if Path(name).suffix.lower() not in engine.music.EXT:
                        return self._json({"error": "只支持 mp3 / wav / flac / m4a / aac / ogg"})
                    if len(raw) > 80 * 1024 * 1024:
                        return self._json({"error": "单个文件不能超过 80MB"})
                    engine.music.dir.mkdir(parents=True, exist_ok=True)
                    (engine.music.dir / re.sub(r'[\\/:*?"<>|]', "_", name)).write_bytes(raw)
                    engine.music._emit()
                    return self._json(engine.music.view())
                if url.path == "/api/live_bg":  # 背景库：上传一张并立即换上
                    name = f"{int(__import__('time').time() * 1000)}.jpg"
                    save_image(raw, LIVE_BG_DIR / name, square=False)
                    save_settings({"live": {"bg": name}})
                    engine.apply_settings({"live": {"bg": name}})
                    return self._json(live_view(engine))
                body = json.loads(raw or b"{}")
                if url.path == "/api/shops":
                    return self._json(shop_action(engine, shops, body))
                if url.path == "/api/shop_ask":
                    return self._json(shop_ask(engine, body["shop"], body["question"]))
                if url.path == "/api/shop_extract":
                    return self._json(shop_extract(engine, body["text"]))
                if url.path == "/api/live":  # 直播设置（接管开关、场景、主题、组件、布局、话术、声音……），改了立刻生效
                    patch, back = mode_look(engine, clean_live_patch(body))
                    save_settings({"live": patch}, replace=tuple(("live", k) for k in ("layout", "phrases", "tests", "theme", "styles", "widgets", "texts", "order", "danmaku", "looks", "activity", "scenes")))
                    engine.apply_settings({"live": patch})
                    if "mode" in patch:
                        engine.show.check()
                    if "show_auto" in patch and engine.cfg["live"].get("mode") != "chat":  # AI 导播开关：换一套系统提示词（接着之前的对话）
                        engine._connect_llm()
                    if back:
                        apply_look(engine, back.get("voice"), back.get("avatar"))
                    elif patch.get("mode") == "chat":  # 第一次开唠嗑：套上当前人设的整套
                        persona_action(engine, personas, {"action": "use", "id": engine.persona.get("id") or personas["personas"][0]["id"]})
                    return self._json(live_view(engine))
                if url.path == "/api/music":  # 背景音乐控制：play / pause / next / prev / select / vol / duck / mode / delete
                    m = engine.music
                    if body.get("action") == "delete":
                        name = Path(str(body.get("name", ""))).name
                        if Path(name).stem == m.title and m.playing:
                            m.control("pause")
                        (m.dir / name).unlink(missing_ok=True)
                        m.idx = min(m.idx, max(0, len(m.files()) - 1))
                        m._emit()
                    else:
                        m.control(str(body.get("action", "")), index=body.get("index", 0), value=body.get("value"))
                        save_settings({"live": {"music": engine.cfg["live"]["music"]}})
                    return self._json(m.view())
                if url.path == "/api/personas":  # 唠嗑人设：保存 / 删除 / 恢复预设 / 使用
                    return self._json(persona_action(engine, personas, body))
                if url.path == "/api/corpus":  # 语料：打分 / 改写理想回答 / 备注 / 删除
                    ids, act = [int(i) for i in body.get("ids", [])][:1000], body.get("action")
                    if act == "delete":
                        engine.corpus.delete(ids)
                    elif act in ("rating", "ideal", "note"):
                        v = body.get("value")
                        engine.corpus.update(ids, **{act: max(-1, min(1, int(v))) if act == "rating" else str(v or "")[:4000]})
                    return self._json({"ok": True})
                if url.path == "/api/corpus_export":  # 按当前筛选条件导出训练数据到 data/exports
                    fmt = body.get("fmt") if body.get("fmt") in ("openai", "sharegpt", "alpaca", "dpo") else "openai"
                    return self._json(engine.corpus.export(body.get("filters") or {}, fmt, ROOT / "data" / "exports",
                                                           anon=bool(body.get("anon", True)), good_only=bool(body.get("good_only", True))))
                if url.path == "/api/danmaku_open":  # 打开抖音直播间网页、开始收弹幕
                    return self._json(open_danmaku_window(engine, str(body.get("url", ""))))
                if url.path == "/api/danmaku_close":
                    return self._json(close_danmaku_window(engine))
                if url.path == "/api/director_test":  # 店铺 → 导播「试一试」：用编辑中的资料（不用先保存）判断这句话画面会怎么切
                    from engine import Showcase
                    import types
                    sc = Showcase(types.SimpleNamespace(shop=clean_shop(body.get("shop") or {}),
                                                    cfg={**engine.cfg, "live": {**engine.cfg["live"], "mode": "shop"}}), run=False)
                    r = sc.plan(str(body.get("text", ""))[:200]) if body.get("text") else {}
                    return self._json({**{k: r.get(k) for k in ("kind", "show", "hints")},
                                       "auto": {c: sc.auto_words(c) for c in sc.cats()},
                                       "groups": {w: [sc.label(i) for i in ids] for w, ids in sc.groups().items()}})
                if url.path == "/api/activity":  # 唠嗑活动：切换（唠嗑 / 跳舞 / 小游戏）、心愿进度清零、积分清零
                    act = body.get("action")
                    if act == "get":
                        pass
                    elif act in ("pause", "resume"):  # 小游戏暂停 / 继续
                        return self._json(engine.act.pause(act == "pause"))
                    elif act == "stop":  # 小游戏终止
                        return self._json(engine.act.stop_game())
                    elif act == "records":  # 小游戏历史记录（最近 30 场）
                        return self._json({"games": engine.act.records()})
                    elif act == "wish_reset":
                        engine.act.reset_wish()
                    elif act == "score_reset":
                        engine.act.scores = {}
                        engine.act.emit()
                    else:
                        return self._json(engine.act.set(str(body.get("act", "talk"))))
                    return self._json(engine.act.view())
                if url.path == "/api/reload":  # 控制台「重新加载」：弹幕窗口在这里重开，其余交给引擎
                    what = str(body.get("what", ""))
                    if what == "danmaku":
                        if not engine.cfg["live"]["danmaku"].get("url"):
                            return self._json({"error": "还没填直播间地址"})
                        close_danmaku_window(engine)
                        return self._json(open_danmaku_window(engine, engine.cfg["live"]["danmaku"]["url"]))
                    return self._json(engine.reload(what))
                if url.path == "/api/danmaku_op":  # 控制台：忽略 / 删除排队的留言
                    return self._json(engine.danmaku.op("delete" if body.get("op") == "delete" else "skip",
                                                         [int(i) for i in body.get("ids", []) if str(i).isdigit()]))
                if url.path == "/api/danmaku_push":  # 其他来源（以后的官方接口、调试）直接推弹幕：{items: [{type, nick, text}]}
                    engine.danmaku.push([i for i in body.get("items", []) if isinstance(i, dict)])
                    return self._json({"ok": True})
                if url.path == "/api/action":  # 控制台点了某个动作：直播画面里的掌柜马上做
                    if body.get("name") not in MOTIONS:
                        return self._json({"error": "没有这个动作"})
                    EV.emit("action", name=body["name"])
                    return self._json({"ok": True})
                if url.path == "/api/live_bg_delete":
                    name = Path(str(body.get("name", ""))).name
                    (LIVE_BG_DIR / name).unlink(missing_ok=True)
                    if engine.cfg["live"]["bg"] == name:
                        save_settings({"live": {"bg": ""}})
                        engine.apply_settings({"live": {"bg": ""}})
                    return self._json(live_view(engine))
                if url.path in ("/api/settings", "/api/llm_test", "/api/llm_models"):
                    llm = body.get("llm", {})
                    llm.pop("api_key", None)  # 只接受端到端加密过的 Key，明文一律丢弃
                    box = llm.pop("api_key_box", None)
                    key = transport.open(box) if box else ""
                    # 没输新 Key 时才用已保存的，且只发给原来的地址：换了服务商不能把旧 Key 发过去
                    saved = engine.cfg["llm"].get("api_key", "") if llm.get("base_url") == engine.cfg["llm"]["base_url"] else ""
                    if url.path == "/api/llm_test":
                        return self._json(llm_test(llm["base_url"], llm["model"], key or saved))
                    if url.path == "/api/llm_models":
                        return self._json(llm_models(llm["base_url"], key or saved))
                    tts = body.get("tts", {})
                    tts.pop("api_key", None)
                    tts_box = tts.pop("api_key_box", None)
                    tts_key = transport.open(tts_box) if tts_box else ""
                    if tts_key:
                        tts["api_key"] = tts_key
                    if llm.get("base_url"):
                        check_base_url(llm["base_url"])
                    if key:
                        llm["api_key"] = key
                    elif llm.get("base_url", engine.cfg["llm"]["base_url"]) != engine.cfg["llm"]["base_url"]:
                        llm.update(api_key_enc="", api_key_sha256="")  # 换了地址又没填新 Key：作废旧 Key
                    save_settings(body)  # Key 在这里被 DPAPI 加密落盘
                    if key:
                        llm["api_key_sha256"] = hashlib.sha256(key.encode()).hexdigest()
                    elif "api_key_enc" in llm:
                        llm["api_key"] = ""
                    if tts_key:
                        tts["api_key_sha256"] = hashlib.sha256(tts_key.encode()).hexdigest()
                    engine.apply_settings(body)  # 明文只进内存
                    return self._json(settings_view(engine))
                if url.path == "/api/voice_design":  # 按文字描述设计一个新的本地参考音色（约 15 秒）
                    from engine import design_voice, ref_voices

                    vid = design_voice(MODELS, str(body.get("name", ""))[:20], str(body.get("desc", ""))[:300])
                    return self._json({"id": vid, "voices": ref_voices(MODELS)})
                if url.path == "/api/export":
                    return self._json({"path": str(engine.export_history())})
                if url.path == "/api/open":  # 只允许打开导出目录
                    os.startfile(ROOT / "data" / "exports")
                    return self._json({"ok": True})
                if url.path == "/api/live_window":  # 给直播伴侣“窗口采集”用的独立直播画面窗口
                    return self._json(open_live_window(engine))
                actions = {
                    "/api/chat": lambda: engine.chat(body.get("text", "")),
                    "/api/ledger_clear": lambda: engine.ledger.clear("all" if body.get("scope") == "all" else "today"),  # 清空 token 账本
                    "/api/showcase": lambda: engine.show.manual(str(body.get("id", "main"))[:30], body.get("pick") if isinstance(body.get("pick"), dict) else None),  # 直播画面切换；pick = 临时组合 {title, ids}
                    "/api/say": lambda: engine.say(str(body.get("text", ""))[:500]),  # 人工接管：让掌柜原样说
                    "/api/viewer": lambda: engine.viewer(str(body.get("name") or "观众")[:20], str(body.get("text", ""))[:300]),  # 模拟观众留言
                    "/api/mic": lambda: engine.set_mic(bool(body.get("on"))),
                    "/api/mode": lambda: engine.set_mode(body.get("mode", "")),
                    # card：首页点的是哪张卡（如 live2d_animal），下次启动标“上次使用”
                    "/api/kind": lambda: (engine.set_kind(body.get("kind", ""), body.get("card")),
                                          save_settings({"avatar_kind": engine.cfg.get("avatar_kind")})),
                    "/api/stop": engine.stop,
                    "/api/clear": engine.clear_history,
                    "/api/voice_preview": lambda: engine.preview_voice(
                        {**{k: body[k] for k in ("engine", "voice", "local_voice", "qwen_voice", "qwen_region", "ref_voice", "rate", "pitch") if k in body},
                         **({"api_key": transport.open(body["api_key_box"])} if body.get("api_key_box") else {})}),
                }
                if url.path not in actions:
                    return self._send(404, b"{}", "application/json")
                actions[url.path]()
                self._json({"ok": True})
            except Exception as e:  # noqa: BLE001
                from engine import log

                msg = re.sub(r"sk-[A-Za-z0-9]{6,}", "sk-***", str(e))  # 报错信息里万一带了 Key 也不外泄
                log(f"[接口] {url.path} 出错：{type(e).__name__}: {msg}", "error")
                self._send(500, json.dumps({"error": msg}, ensure_ascii=False).encode(), "application/json")

    return Handler


WINDOWED = False  # run() 用 pywebview 开窗口时为真：直播画面可以开成程序自己的独立窗口


def open_live_window(engine) -> dict:
    """直播画面窗口（给直播伴侣“窗口采集”用）。画面设置都在服务端，地址只带令牌；浏览器调试模式下返回地址，由页面自己打开。"""
    if not WINDOWED:
        return {"url": engine.live_url}
    import webview

    c = engine.cfg["live"]
    short = int(c.get("res", "1080"))
    pw, ph = (short * 16 // 9, short) if c["scene"] == "plain" and c["ratio"] == "16:9" else (short, short * 16 // 9)  # 要的物理像素（画面内容）
    # 窗口采集采到的是窗口的物理像素：按系统缩放换算成 pywebview 用的逻辑尺寸；比屏幕还大的开不出来，等比缩到放得下
    hdc = ctypes.windll.user32.GetDC(0)
    sw, sh = ctypes.windll.gdi32.GetDeviceCaps(hdc, 118), ctypes.windll.gdi32.GetDeviceCaps(hdc, 117)  # DESKTOPHORZRES / VERTRES：真实分辨率
    ctypes.windll.user32.ReleaseDC(0, hdc)
    fit = min(1, sw * .96 / pw, sh * .9 / ph)
    tw, th = int(pw * fit) // 2 * 2, int(ph * fit) // 2 * 2
    scale = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100
    win = webview.create_window("AI虚拟主播 直播画面", engine.live_url, width=int(tw / scale) + 16, height=int(th / scale) + 39,
                                background_color="#00ff00" if c["scene"] == "plain" and c["bgmode"] == "green" else "#000000")

    def exact(*_):  # 加载后量一下内容区实际有多少物理像素，把边框标题栏的差补上，让采到的正好是 tw × th
        try:
            iw, ih, dpr = win.evaluate_js("[innerWidth, innerHeight, devicePixelRatio]")
            win.resize(int(win.width + (tw - iw * dpr) / dpr), int(win.height + (th - ih * dpr) / dpr))
        except Exception as e:  # noqa: BLE001
            print(f"[直播] 调整窗口尺寸失败：{e}", flush=True)

    win.events.loaded += exact
    note = "" if fit == 1 else f"屏幕放不下 {pw}×{ph} 的窗口，已开成 {tw}×{th}；要 {short}P 请用 OBS 浏览器源（宽 {pw} 高 {ph}）"
    return {"ok": True, "size": f"{tw}×{th}", "note": note}


# ---------- 抖音弹幕：在程序自己的浏览器窗口里打开直播间网页，读页面上显示的评论和“XX 来了”，经 pywebview 的 JS 桥交给掌柜。
# 只读网页上公开显示的内容；抖音改版网页时要跟着改选择器（webcast-chatroom___item 等）
DANMAKU_JS = r"""(() => {
  if (window.__aiDanmaku) return; window.__aiDanmaku = true;
  const seen = new Map(), gifted = new Map(); let buf = [], room = null, mo = null;
  const add = it => { const k = it.type + '|' + it.nick + '|' + it.text, now = Date.now(); if (now - (seen.get(k) || 0) < 60000) return; seen.set(k, now); buf.push(it); };
  // 昵称：取「：」前面那段文字（昵称前的等级 / 粉丝团徽章是单独的元素，里面的数字不算昵称）
  const nickOf = el => {
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT); let prev = '', n;
    while ((n = w.nextNode())) { const t = n.textContent, k = t.indexOf('：'); if (k >= 0) return (t.slice(0, k).trim() || prev).trim(); if (t.trim()) prev = t.trim(); }
    return '';
  };
  const handle = el => {  // 一条：「昵称：内容」/「昵称 来了」/「昵称：为主播点赞了」/「昵称：送出了 [图] 小心心 × 1」
    const raw = el.innerText.trim(); if (!raw) return;
    const c = el.querySelector('.webcast-chatroom___content-with-emoji-text'), i = raw.indexOf('：'), flat = raw.replace(/\s+/g, ' ');
    const gift = flat.match(/送出了?\s*(.*?)\s*[×xX*]\s*(\d+)\s*$/);
    if (gift && i >= 0) {
      // 辨真假：观众自己打字「送出了 小心心 ×1」是普通评论——文字在评论内容框里、颜色和评论一样（白 / 灰）；
      // 真礼物是系统消息，「送出了」那几个字是暖色（橙 / 黄）。按颜色判断，不是暖色就记成假礼物（不记账、不道谢、不回答），控制台写上颜色方便核对
      const sEl = [...el.querySelectorAll('*')].find(n => [...n.childNodes].some(t => t.nodeType === 3 && /送/.test(t.textContent))) || el;
      const [r, g, b] = (getComputedStyle(sEl).color.match(/\d+/g) || [255, 255, 255]).map(Number), warm = r >= 180 && g >= 90 && r - b >= 70;
      const typed = !!(c && c.contains(sEl)), color = `rgb(${r},${g},${b})`;
      if (!warm) { add({type: 'fakegift', nick: nickOf(el) || raw.slice(0, i).trim(), text: flat.slice(i + 1, i + 40).trim(), why: (typed ? '打字的 ' : '') + color}); return; }
      const img = el.querySelector('img[alt]:not([alt=""]), img[title]:not([title=""])');
      // 连击时同一条消息会原地从「× 1」变成「× 5」：按消息编号记住已经算过的个数，只记新增的
      const id = el.dataset.id || el.querySelector('[data-id]')?.dataset.id || flat, n = +gift[2], had = gifted.get(id) || 0;
      if (n > had) { gifted.set(id, n); buf.push({type: 'gift', nick: nickOf(el) || raw.slice(0, i).trim(), text: (gift[1] || (img && (img.alt || img.title)) || '礼物').trim(), n: n - had}); }
      return;
    }
    if (!c && i < 0) { const m = raw.match(/^(.+?)\s*来了$/); add(m ? {type: 'enter', nick: m[1], text: ''} : {type: 'other', nick: '', text: flat.slice(0, 80)}); return; }
    // 内容用内容所在的元素来切（昵称里带「：」也不会切错）；昵称前面多出来的徽章数字去掉
    const text = (c ? c.innerText : raw.slice(i + 1)).trim(), cut = c && text ? raw.lastIndexOf(text) : i;
    let nick = raw.slice(0, cut > 0 ? cut : i).replace(/[：:\s]+$/, '').trim();
    const n2 = nickOf(el); if (n2 && n2 !== nick && nick.endsWith(n2)) nick = n2;
    add(/^为主播点赞了/.test(text) ? {type: 'like', nick, text: ''} : {type: 'comment', nick, text});
  };
  const watch = () => {
    const r = document.querySelector('.webcast-chatroom');
    if (r && r !== room) {  // 列表会复用旧节点，所以新加的子节点也要往上找到所在的那一条
      room = r; if (mo) mo.disconnect();
      mo = new MutationObserver(ms => { for (const m of ms) for (const n of m.addedNodes) {
        const el = n.nodeType === 1 ? n : n.parentElement; if (!el) continue;
        const it = el.closest('.webcast-chatroom___item');
        (it ? [it] : el.querySelectorAll('.webcast-chatroom___item')).forEach(handle);
      } });
      mo.observe(r, {childList: true, subtree: true, characterData: true});
    }
    document.querySelectorAll('video').forEach(v => { v.muted = true; });  // 只收评论，不出声
  };
  setInterval(watch, 1000);
  setInterval(() => { const a = window.pywebview && window.pywebview.api; if (!a) return; const items = buf; buf = []; a.push(items, {room: !!room, title: document.title}); }, 700);
  setInterval(() => { const now = Date.now(); for (const [k, t] of seen) if (now - t > 120000) seen.delete(k); if (gifted.size > 500) gifted.clear(); }, 30000);
})();"""
DANMAKU_WIN = None


class DanmakuBridge:
    """弹幕窗口里的页面只能调用这一个方法（pywebview 会把公开属性都暴露给页面，所以引擎放在下划线属性里）。"""

    def __init__(self, engine):
        self._engine = engine

    def push(self, items, info=None):
        d = self._engine.danmaku
        d.source.update(state="on" if (info or {}).get("room") else "waiting", title=str((info or {}).get("title", ""))[:60], t=time.time())
        if items:
            d.push([i for i in items if isinstance(i, dict)])


def danmaku_url(raw: str) -> str:
    """直播间地址：完整网址、分享文字里的链接，或只填数字房间号都行；只允许抖音的域名。"""
    m = re.search(r"https?://[^\s，。]+", raw)
    url = m.group(0) if m else f"https://live.douyin.com/{raw.strip()}" if raw.strip().isdigit() else ""
    host = re.match(r"https?://([^/:?#]+)", url)
    if not host or not re.search(r"(^|\.)douyin\.com$", host.group(1)):
        raise ValueError("请填抖音直播间地址，例如 https://live.douyin.com/123456789，或直接填房间号")
    return url


def open_danmaku_window(engine, raw: str) -> dict:
    global DANMAKU_WIN
    import time as _t

    url = danmaku_url(raw)
    save_settings({"live": {"danmaku": {**engine.cfg["live"]["danmaku"], "url": raw.strip()}}}, replace=(("live", "danmaku"),))
    engine.cfg["live"]["danmaku"]["url"] = raw.strip()
    if not WINDOWED:
        return {"error": "浏览器调试模式下打不开弹幕窗口，请用 AI虚拟主播.exe 启动"}
    import webview

    engine.danmaku.source.update(state="waiting", title="", t=_t.time())
    if DANMAKU_WIN:
        DANMAKU_WIN.load_url(url)
        DANMAKU_WIN.show()
        return {"ok": True}
    w = webview.create_window("抖音弹幕接收（可以最小化，别关闭）", url, js_api=DanmakuBridge(engine), width=460, height=820)

    def closed(*_):
        global DANMAKU_WIN
        DANMAKU_WIN = None
        engine.danmaku.source.update(state="off", t=_t.time())

    w.events.loaded += lambda *_: w.evaluate_js(DANMAKU_JS)
    w.events.closed += closed
    DANMAKU_WIN = w
    return {"ok": True}


def close_danmaku_window(engine) -> dict:
    if DANMAKU_WIN:
        DANMAKU_WIN.destroy()
    engine.danmaku.source.update(state="off")
    return {"ok": True}


def fetch_cubism_core():
    p = ROOT / "third_party" / "web" / CUBISM_CORE
    if not p.exists():
        import httpx

        r = httpx.get(CUBISM_URL, follow_redirects=True, timeout=60)
        r.raise_for_status()
        save_text(p, r.text)


def serve(port=None):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from engine import Engine, log

    cfg = load_config()
    shops = load_shops(cfg)
    engine = Engine(cfg, ROOT)
    engine.shop = {}  # 这个版本没有店铺
    personas = load_personas()
    engine.persona = next((x for x in personas["personas"] if x["id"] == cfg["live"].get("persona")), personas["personas"][0])
    engine.start()
    token = secrets.token_urlsafe(24)  # 每次启动随机生成，只有本程序窗口知道
    handler = make_handler(engine, token, Transport(), shops, personas)
    try:  # 固定端口：OBS 里填的直播画面地址每次启动都一样
        srv = ThreadingHTTPServer(("127.0.0.1", engine.cfg["live"]["port"] if port is None else port), handler)
    except OSError:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        log(f"[直播] 端口 {engine.cfg['live']['port']} 被占用（是不是开了两个程序？），这次改用 {srv.server_address[1]}，"
            "OBS 里的直播画面地址要跟着改", "warn")
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    p = srv.server_address[1]
    engine.live_url = f"http://127.0.0.1:{p}/live?k={engine.cfg['live']['token']}"
    print(f"[serve] http://127.0.0.1:{p}/（访问需令牌）", flush=True)
    return f"http://127.0.0.1:{p}/?t={token}"


MAIN_TITLE = "AI虚拟主播"


def message(text: str, flags: int = 0x40):
    ctypes.windll.user32.MessageBoxW(0, text, "AI虚拟主播", flags | 0x10000)  # 0x10000：弹到最前面


def single_instance() -> bool:
    """只开一个程序：已经开着就把窗口叫到前面。两个程序会抢端口和 WebView 数据目录，后开的那个打不开。"""
    global _MUTEX
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    _MUTEX = k32.CreateMutexW(None, False, "AIVirtualStreamer.SingleInstance")  # 留着句柄，程序退出时系统回收
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return True
    hwnd = ctypes.windll.user32.FindWindowW(None, MAIN_TITLE)
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 9)  # 最小化了就还原
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    else:
        message("程序正在启动或退出中，请等几秒再试。\n一直打不开的话，到任务管理器里结束 AI虚拟主播.exe 再启动。")
    return False




def run():
    global WINDOWED
    if not single_instance():
        return
    import webview

    url = serve()
    WINDOWED = True
    # WebView2 关掉后台节流：弹幕窗口最小化 / 被挡住时照样更新；直播画面窗口被挡住时也不掉帧（直播伴侣窗口采集）
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                          "--disable-background-timer-throttling --disable-renderer-backgrounding --disable-backgrounding-occluded-windows")
    win = webview.create_window(MAIN_TITLE, url, width=1440, height=900, min_size=(1100, 700), background_color="#f5f5f7")
    webview.start(private_mode=False, storage_path=str(ROOT / "data" / "webview"))  # 保留登录状态：弹幕窗口里登录过抖音下次不用再登
    os._exit(0)  # 关窗即退出，不等后台线程


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "serve":
        print(f"[serve] 用浏览器打开（含本次令牌）：{serve(int(sys.argv[2]) if len(sys.argv) > 2 else 8765)}", flush=True)
        threading.Event().wait()
    else:
        try:
            run()
        except Exception as e:  # noqa: BLE001  没有控制台：出错要弹窗说出来，不能默默退出
            import traceback

            traceback.print_exc()
            message(f"启动失败：{type(e).__name__}: {e}\n\n详细信息在 {ROOT / 'logs' / 'app.log'}", 0x10)
