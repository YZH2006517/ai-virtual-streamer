"""AI 唠嗑主播的引擎：弹幕 / 文字 → 大模型流式分句 → Edge 语音 → Live2D 口型时间轴 + 播放；外加背景音乐、活动、账本。
"""
import asyncio
import base64
import json
import math
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import types
import unicodedata
from collections import deque
from pathlib import Path

from safefile import read_json, save_text

import numpy as np

KINDS = {"live2d": "Live2D"}
# Edge TTS（微软在线）里会说中文的音色 → 界面上显示的名字（配合“音调”可调出卡通 / 小动物声）
VOICES = {
    "zh-CN-XiaoxiaoNeural": "晓晓 · 温柔女声", "zh-CN-XiaoyiNeural": "晓伊 · 活泼女声（卡通）",
    "zh-CN-YunxiNeural": "云希 · 阳光男声", "zh-CN-YunjianNeural": "云健 · 激情男声（体育）",
    "zh-CN-YunyangNeural": "云扬 · 新闻男声", "zh-CN-YunxiaNeural": "云夏 · 可爱少年音（卡通）",
    "zh-CN-liaoning-XiaobeiNeural": "晓北 · 东北话（幽默）", "zh-CN-shaanxi-XiaoniNeural": "晓妮 · 陕西话",
    "zh-TW-HsiaoChenNeural": "曉臻 · 台湾腔女声", "zh-TW-HsiaoYuNeural": "曉雨 · 台湾腔女声", "zh-TW-YunJheNeural": "雲哲 · 台湾腔男声",
    "zh-HK-HiuGaaiNeural": "曉佳 · 粤语女声", "zh-HK-HiuMaanNeural": "曉曼 · 粤语女声", "zh-HK-WanLungNeural": "雲龍 · 粤语男声",
    "en-US-AvaMultilingualNeural": "Ava · 外国口音中文（女）", "en-US-AndrewMultilingualNeural": "Andrew · 外国口音中文（男）",
}
TTS_ENGINES = {"edge": "Edge 在线音色"}
SR = 16000      # VAD / ASR / FlashHead 输入音频
OUT_SR = 24000  # TTS 输出与扬声器播放
FPS = 25
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # 子进程不弹黑框

# 主程序（main.py）沿用的接口：这个版本只有 Edge 在线语音
QWEN_VOICES, MODES = {}, {}


class LocalTTS:
    @staticmethod
    def voices(models): return []

    @staticmethod
    def names(models): return {}


class SovitsTTS:
    @staticmethod
    def python(root): return None


def ref_voices(models) -> dict:
    return {}


def design_voice(*a):
    raise RuntimeError("这个版本没有音色设计")


def build_system(cfg: dict, shop: dict) -> str:
    return build_chat_system(cfg)


def release_tts(engine: str): ...


def make_tts(cfg: dict, models: Path):
    return TTS(cfg["voice"], cfg["rate"], cfg.get("pitch", "+0Hz"))


# ============================================================ 事件总线：日志 / 对话 / 流程耗时 → 界面

class Events:
    def __init__(s):
        s.subs, s.lock, s.history = [], threading.Lock(), deque(maxlen=400)

    def emit(s, kind, **data):
        e = {"kind": kind, "t": time.time(), **data}
        with s.lock:
            if kind not in ("sys", "chat", "speak", "avatar", "shop", "live", "action", "notice", "danmaku", "music", "ledger", "showcase", "showfocus", "showsection", "showpace", "reload", "activity", "beats"):  # 对话记录由 /api/history 提供、口型和形象只对当下有效，都不重放
                s.history.append(e)
            for q in s.subs:
                q.put(e)
        if kind == "log":
            print(time.strftime("%H:%M:%S"), data.get("msg"), flush=True)

    def subscribe(s):
        q = queue.Queue()
        with s.lock:
            s.subs.append(q)
            return q, list(s.history)

    def unsubscribe(s, q):
        with s.lock:
            s.subs.remove(q)


EV = Events()


def log(msg, level="info"):
    EV.emit("log", msg=msg, level=level)


# ============================================================ LLM / TTS


def build_chat_system(cfg: dict, persona: dict | None = None) -> str:
    """唠嗑直播：纯陪聊的主播人设（data/personas.json 里选的那个），不带店铺资料、不推销。"""
    persona = persona or {}
    name = persona.get("name") or cfg.get("name", "悠悠")
    style = persona.get("style") or cfg["persona"].strip()
    topics = "、".join(persona.get("topics") or [])
    return (f"你是「{name}」，一个 AI 数字人主播。你的性格和说话方式：{style}\n"
            + (f"你平时爱聊的话题：{topics}。\n" if topics else "")
            + "\n现在你在抖音直播间做唠嗑主播，陪观众聊天解闷，不卖东西、不讲任何店铺和商品。始终保持上面的性格和说话方式。\n\n"
            "怎么聊：\n"
            "- 像有趣的真人主播那样接话：接梗、调侃、说说自己的看法和日常小事，偶尔反问观众一句，把话题聊下去。\n"
            "- 观众问知识类的问题就简单说清楚；不确定的就直说不确定，不编。\n"
            "- 观众只打招呼或者没什么可聊时，主动抛一个轻松的话题（吃的、天气、周末、宠物、电影剧集、生活小技巧……），每次换不一样的。\n"
            "- 不推销、不提店铺和商品、不引导下单或点链接。\n"
            "- 你是 AI 数字人，被问到就大方承认，不假装真人。\n"
            "- 遵守直播平台规范：不聊政治、色情、暴力、赌博，不给医疗、投资、法律方面的具体建议，不打听观众隐私；碰到这类话题就轻松岔开。\n"
            "- 每次换着说法回答，不重复自己之前的句子和开场白。\n"
            "- 每次 1 到 3 句，口语化，不用书面语，不列清单。")


class LLM:
    def __init__(s, cfg: dict, system: str):
        from openai import OpenAI

        key = cfg.get("api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
        if not key and re.search(r"//(127\.0\.0\.1|localhost)[:/]", cfg["base_url"]):
            key = "local"  # 本机模型（如 Ollama）不需要 Key
        if not key:
            raise RuntimeError("未配置大模型 API Key：在「设置 → 大模型」里填写")
        s.client, s.model, s.system, s.history = OpenAI(api_key=key, base_url=cfg["base_url"]), cfg["model"], system, []
        s.on_usage = None  # 每轮回答的 token 用量 → Ledger.add（接口在最后一个分片里给 usage）

    def sentences(s, text: str, cancelled=lambda: False, keep=True):
        """流式生成，逐句产出；首句在逗号处提前切，缩短开口延迟。"""
        # 每次在最新的问题前带上现在的日期、星期、时间：否则大模型不知道今天是工作日还是周末，会说错票价和营业状态；
        # 放在最新消息里而不是系统提示里，才压得住上下文里之前说错的话（历史里存的仍是原话）
        lt = time.localtime()  # 日期自己拼：Windows 上 strftime 格式里带中文会被吞掉，模型就只看到“一（工作日）”
        now = f"{lt.tm_year}年{lt.tm_mon}月{lt.tm_mday}日 星期{'一二三四五六日'[lt.tm_wday]}{'（周末）' if lt.tm_wday >= 5 else '（工作日）'} {lt.tm_hour:02d}:{lt.tm_min:02d}"
        msgs = [{"role": "system", "content": s.system}, *s.history[-12:], {"role": "user", "content": f"【当前时间：{now}】{text}"}]
        s.last_msgs = msgs  # 语料库要存“模型这一轮实际看到的全部内容”
        stream = s.client.chat.completions.create(model=s.model, messages=msgs, stream=True, temperature=0.9,
                                                  stream_options={"include_usage": True})
        buf, full, first = "", "", True
        for chunk in stream:
            if cancelled():
                stream.close()
                break
            if getattr(chunk, "usage", None) and s.on_usage:
                s.on_usage(chunk.usage, getattr(chunk, "model", "") or s.model)
            if chunk.choices:
                buf += chunk.choices[0].delta.content or ""
            while True:
                o = max(buf.rfind("〔"), buf.rfind("【"))
                scan = buf[:o] if o > max(buf.rfind("〕"), buf.rfind("】")) else buf  # 导播指令还没写完：只在它前面找句号
                m = re.search(r"[。！？!?；;\n，,]" if first else r"[。！？!?；;\n]", scan)
                if not m or (first and m.group() in "，," and m.start() < 6):
                    break
                sent, buf = buf[:m.end()].strip(), buf[m.end():]
                if sent:
                    full += sent
                    first = False
                    yield sent
        if buf.strip() and not cancelled():
            full += buf.strip()
            yield buf.strip()
        if keep:  # keep=False：控制台测试留言，不进上下文
            s.history += [{"role": "user", "content": text}, {"role": "assistant", "content": full}]


def clean_for_speech(text: str) -> str:
    return re.sub(r"[〔【][^〔〕【】]*[〕】]|[*#`>_~\[\]]|[\U00010000-\U0010ffff]", "", text)  # markdown 符号、emoji 不念


class TTS:
    """Edge TTS（微软在线音色；不能克隆）。返回 24k 单声道 float32。pitch 如 "+30Hz"：调高像卡通 / 小动物。"""

    def __init__(s, voice: str, rate: str, pitch: str = "+0Hz"):
        import imageio_ffmpeg

        s.voice, s.rate, s.pitch, s.ffmpeg = voice, rate, pitch, imageio_ffmpeg.get_ffmpeg_exe()

    def __call__(s, text: str, words: list | None = None) -> np.ndarray:
        """words 传入列表时，顺便收集每个词的发音时间 (起点秒, 时长秒, 词)，给 Live2D / VRM 对口型。"""
        import edge_tts

        text = clean_for_speech(text)

        async def fetch():
            buf = bytearray()
            async for c in edge_tts.Communicate(text, s.voice, rate=s.rate, pitch=s.pitch, boundary="WordBoundary").stream():
                if c["type"] == "audio":
                    buf += c["data"]
                elif words is not None and c["type"] == "WordBoundary":
                    words.append((c["offset"] / 1e7, c["duration"] / 1e7, c["text"]))
            return bytes(buf)

        mp3 = asyncio.run(fetch())
        pcm = subprocess.run([s.ffmpeg, "-v", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", str(OUT_SR), "pipe:1"],
                             input=mp3, capture_output=True, check=True, creationflags=NO_WINDOW).stdout
        return np.frombuffer(pcm, np.float32).copy()


QWEN_ERRORS = {  # 百炼常见错误码 → 给店主看的说明
    "Arrearage": "阿里云账号欠费或余额不足，请到阿里云控制台充值，并确认已开通「大模型服务平台百炼」",
    "InvalidApiKey": "API Key 无效，请到百炼控制台「API-KEY」重新复制（注意不要多空格）",
    "AccessDenied": "这个 Key 没有权限使用语音合成，请在百炼控制台确认已开通 qwen3-tts-flash",
    "Model.AccessDenied": "这个 Key 没有权限使用语音合成，请在百炼控制台确认已开通 qwen3-tts-flash",
    "Throttling": "请求太频繁或超出免费额度，稍后再试",
    "Throttling.RateQuota": "请求太频繁，超出了每分钟的调用限制",
    "Throttling.AllocationQuota": "免费额度或配额已用完",
}


QWEN_URL = {"cn": "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",  # 阿里云百炼 北京
            "intl": "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"}  # 新加坡（国际站）


VOICE_REF_TEXT = "大家好呀，欢迎来到直播间！今天想问什么都可以直接发弹幕问我哦，我会一条一条认真回答的。"
VOICE_PRESETS = {"yuanqi": ("元气少女", "十六七岁的二次元少女，声音清亮甜美、元气满满，语速轻快，句尾微微上扬，笑意明显，像动画里活泼开朗的女主角"),
                 "ruanmeng": ("软萌妹妹", "二次元软萌少女音，音调偏高，声音软糯带一点奶气，说话轻柔可爱，有撒娇感，咬字清楚"),
                 "wenrou": ("温柔学姐", "二十岁左右的二次元温柔学姐，音色清澈柔和，语速平稳，亲切治愈，带着微笑说话")}


# ============================================================ 口型时间轴（Live2D / VRM：不生成画面，只告诉前端每一刻嘴张多大、什么口型）

TRACK_FPS = 50
_DIGIT_VOWEL = dict(zip("0123456789", "iieaiuuiau"))  # 零一二三四五六七八九 的主元音


def _vowels(word: str) -> list:
    """一个词 → 每个音节的口型 a/i/u/e/o：汉字取拼音韵母的主元音，英文按元音字母，数字按中文读法。"""
    from pypinyin import Style, lazy_pinyin

    out = []
    for f in lazy_pinyin(word, style=Style.FINALS, strict=False, errors=lambda x: list(x)):
        f = f.lower()
        if len(f) == 1 and not f.isalpha():
            if f in _DIGIT_VOWEL:
                out.append(_DIGIT_VOWEL[f])
            continue
        if len(f) == 1 and f not in "aeiou":  # 英文辅音字母不单独成音节
            continue
        out.append(next((v for v in "aoe" if v in f), "u" if "u" in f or "v" in f else "i"))
    return out


def mouth_track(pcm: np.ndarray, words: list, text: str) -> dict:
    """张嘴幅度 = 音量包络（每 20ms 的 RMS，按本句归一）；口型 = 按 Edge TTS 的词时间戳把音节均分到时间轴上。"""
    hop = OUT_SR // TRACK_FPS
    n = len(pcm) // hop + 1
    rms = np.sqrt(np.array([np.mean(pcm[i * hop:(i + 2) * hop] ** 2) if i * hop < len(pcm) else 0.0 for i in range(n)]))
    top = np.percentile(rms[rms > 0], 95) if np.any(rms > 0) else 1.0
    level = np.clip(rms / (top + 1e-6), 0, 1)
    opening = np.where(level < 0.08, 0, level ** 0.7)  # 静音段闭嘴，开方让小声时嘴也张得开
    vowel = ["a"] * n
    if not words:  # 没有词时间戳（本地 TTS）：把音节按顺序铺在“有声音”的帧上，停顿处不占音节
        vs, voiced = _vowels(text) or ["a"], np.flatnonzero(opening > 0)
        for k, i in enumerate(voiced):
            vowel[i] = vs[min(len(vs) - 1, k * len(vs) // len(voiced))]
    for t, d, w in words:
        vs = _vowels(w) or ["a"]
        for j, v in enumerate(vs):
            a, b = int((t + d * j / len(vs)) * TRACK_FPS), int((t + d * (j + 1) / len(vs)) * TRACK_FPS)
            vowel[a:max(b, a + 1)] = [v] * (max(b, a + 1) - a)
    return {"fps": TRACK_FPS, "open": [round(x * 100) for x in opening], "vowel": "".join(vowel[:n])}


FFHQ_5PT = np.array([[0.37691676, 0.46864664], [0.62285697, 0.46912813], [0.50123859, 0.61331904],
                     [0.39308822, 0.72541100], [0.61150205, 0.72490465]], np.float32)  # 双眼/鼻尖/两嘴角


# ============================================================ 声音出口：直播声音设备 + 本机监听

class AudioOut:
    """数字人的声音从这里出去。直播时选一个虚拟声卡（如 VB-CABLE），直播伴侣 / OBS 把它当麦克风采集；
    同时可以在本机默认喇叭上监听。没选直播设备时就只走默认喇叭。"""

    def __init__(s, device="", monitor=True):
        s.device, s.monitor, s.streams, s.lock = device, monitor, [], threading.Lock()

    @staticmethod
    def devices() -> list:
        """可选的输出设备名。只列默认驱动类型（通常是 MME）下的，避免同一个设备在 MME / WASAPI 等里重复出现。"""
        import sounddevice as sd

        api = sd.query_devices(kind="output")["hostapi"]
        return [d["name"] for d in sd.query_devices() if d["max_output_channels"] > 0 and d["hostapi"] == api
                and not re.match(r"Microsoft (声音映射器|Sound Mapper)", d["name"])]  # “声音映射器”就是系统默认设备，不单列

    def _targets(s) -> list:
        """要同时播放的设备：[直播设备, 默认喇叭]；None = 系统默认输出。第一个的延迟用来对齐口型。"""
        import sounddevice as sd

        api = sd.query_devices(kind="output")["hostapi"]
        live = next((i for i, d in enumerate(sd.query_devices()) if s.device and d["name"] == s.device
                     and d["hostapi"] == api and d["max_output_channels"] > 0), None)
        if s.device and live is None:
            log(f"[声音] 找不到直播声音设备「{s.device}」，改用默认喇叭", "warn")
        return [None] if live is None else [live] + ([None] if s.monitor else [])

    def play(s, pcm: np.ndarray) -> float:
        """开始播放（不阻塞），返回第一个设备的输出延迟（秒）。"""
        import sounddevice as sd

        s.stop()
        streams = []
        for dev in s._targets():
            pos = [0]

            def cb(out, frames, _t, _status, pos=pos):
                n = max(0, min(frames, len(pcm) - pos[0]))
                out[:n, 0] = pcm[pos[0]:pos[0] + n]
                out[n:] = 0
                pos[0] += n
                if n < frames:
                    raise sd.CallbackStop

            st = sd.OutputStream(samplerate=OUT_SR, channels=1, dtype="float32", device=dev, callback=cb)
            st.start()
            streams.append(st)
        with s.lock:
            s.streams = streams
        return streams[0].latency

    def stop(s):
        with s.lock:
            streams, s.streams = s.streams, []
        for st in streams:
            st.abort()
            st.close()

    def wait(s):
        while any(st.active for st in list(s.streams)):
            time.sleep(0.02)


class Music:
    """背景音乐：data/music 里的歌按列表播放，和掌柜的声音走同一组输出设备（直播声卡 + 本机监听），直播伴侣 / OBS 采到的是混好的声音；
    掌柜说话时音量平滑压低（duck），说完再升回来。48kHz 立体声单独一路流，不影响口型对齐。"""

    SR, EXT = 48000, (".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg")
    MODES = {"list": "列表循环", "one": "单曲循环", "shuffle": "随机播放"}

    def __init__(s, engine):
        s.e, s.dir = engine, engine.data / "music"
        c = engine.cfg["live"].setdefault("music", {})
        s.vol, s.duck, s.mode = c.get("vol", .35), c.get("duck", .25), c.get("mode", "list")
        s.idx, s.playing, s.pcm, s.ver, s.pos, s.gain = 0, False, None, 0, 0, 0.0
        s.streams, s.lock, s._ending = [], threading.Lock(), False
        s.req, s.by, s.title = [], "", ""  # 点歌队列 [{"path", "title", "nick"}]；正在放的这首是谁点的（空 = 歌单里自己放的）；歌名
        s.cache = engine.data / "music_cache"  # 网上找来的点播歌（不进歌单，只留最近 60 首）
        s.beats = None  # 当前这首的节拍 {ver, title, bpm, beats[秒], energy[每 0.1 秒 0~1]}：直播画面跳舞跟着它

    def files(s) -> list:
        return sorted(p.name for p in s.dir.glob("*") if p.suffix.lower() in s.EXT) if s.dir.exists() else []

    def view(s) -> dict:
        fs = s.files()
        return {"list": fs, "idx": s.idx, "title": s.title, "playing": s.playing,
                "pos": s.pos / s.SR, "dur": len(s.pcm) / s.SR if s.pcm is not None else 0, "t": time.time(),
                "vol": s.vol, "duck": s.duck, "mode": s.mode, "by": s.by,
                "req": [{"title": r["title"], "nick": r["nick"]} for r in s.req]}

    @staticmethod
    def _norm(t: str) -> str:
        return re.sub(r"[\s\W_]+", "", t.lower())

    def find(s, query: str) -> str | None:
        """歌名 → 歌单里的文件。文件名常是「歌手 - 歌名」：整名或其中一段包含 / 等于点的歌名都算，最后按相似度兜底。"""
        import difflib

        q = s._norm(query)
        if not q:
            return None
        best, score = None, 0.0
        for f in s.files():
            parts = [s._norm(Path(f).stem)] + [s._norm(x) for x in re.split(r"\s*[-－—_|]\s*", Path(f).stem)]
            for p in filter(None, parts):
                sc = 1.0 if p == q else .9 if q in p or (p in q and len(p) >= 2) else difflib.SequenceMatcher(None, q, p).ratio()
                if sc > score:
                    best, score = f, sc
        return best if score >= .6 else None

    def _queue(s, path: Path, title: str, nick: str, cut: bool) -> dict:
        if any(r["path"] == path for r in s.req) or (s.by and s.title == title):
            return {"ok": True, "title": title, "dup": True}
        if len(s.req) >= 10:
            return {"ok": False, "full": True, "title": title}
        s.req.append({"path": path, "title": title, "nick": nick})
        now = cut and (not s.playing or not s.by)
        if now:
            threading.Thread(target=lambda: s.control("next"), daemon=True).start()
        s._emit()
        return {"ok": True, "title": title, "now": now, "pos": len(s.req)}

    def request(s, query: str, nick: str, cut: bool = True, online: bool = False, done=None) -> dict:
        """观众点歌：先在歌单里找，找到就排进点歌队列（当前放的不是点播的歌就直接切过去）。
        歌单里没有、又开了「网上找歌」：后台去网易云搜能完整播放的版本，下载好再排队，结果通过 done(结果) 告诉弹幕那边。"""
        f = s.find(query)
        if f:
            return s._queue(s.dir / f, Path(f).stem, nick, cut)
        if not online:
            return {"ok": False, "query": query}

        def run():
            try:
                hit = s.fetch_online(query)
            except Exception as e:  # noqa: BLE001
                log(f"[点歌] 网上找歌出错：{type(e).__name__}: {e}", "warn")
                hit = None
            if done:
                done(s._queue(*hit, nick, cut) if hit else {"ok": False, "query": query, "online": True})
        threading.Thread(target=run, daemon=True).start()
        return {"ok": None, "searching": True, "query": query}

    def fetch_online(s, query: str) -> tuple[Path, str] | None:
        """网易云音乐网页版的公开接口（非官方，随时可能变）：搜歌，挑第一首能完整播放的（会员歌、没版权的拿不到），下到 music_cache。"""
        import httpx

        c = httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"})
        songs = c.get("https://music.163.com/api/cloudsearch/pc", params={"s": query, "type": 1, "limit": 20}).json().get("result", {}).get("songs", [])
        q = s._norm(re.sub(r"\s+", "", query))
        songs.sort(key=lambda x: (s._norm(x["name"]) != q and not q.startswith(s._norm(x["name"])), 0))  # 歌名对得上的排前面
        for x in songs:
            pr = x.get("privilege") or {}
            if pr.get("st", -1) < 0 or not pr.get("pl"):
                continue
            title = f'{x["ar"][0]["name"] if x.get("ar") else "未知"} - {x["name"]}'
            path = s.cache / (re.sub(r'[\\/:*?"<>|]', "_", title)[:80] + ".mp3")
            if not path.exists():
                r = c.get(f"https://music.163.com/song/media/outer/url?id={x['id']}.mp3")
                if not r.headers.get("content-type", "").startswith("audio") or len(r.content) < 300_000:  # 拿到的是网页或几十秒试听
                    continue
                s.cache.mkdir(parents=True, exist_ok=True)
                path.write_bytes(r.content)
                old = sorted(s.cache.glob("*.mp3"), key=lambda p: p.stat().st_mtime)[:-60]
                for o in old:
                    o.unlink(missing_ok=True)
            log(f"[点歌] 网上找到：{title}")
            return path, title
        return None

    def _emit(s):
        EV.emit("music", **s.view())

    def _load(s, i):
        """i：歌单里的序号；或者点歌队列里的一项 {"path", "title"}。"""
        import imageio_ffmpeg

        if isinstance(i, dict):
            path, s.title = i["path"], i["title"]
        else:
            fs = s.files()
            if not fs:
                raise RuntimeError("音乐库是空的，先上传几首歌")
            s.idx = i % len(fs)
            path, s.title = s.dir / fs[s.idx], Path(fs[s.idx]).stem
        raw = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "2",
                              "-ar", str(s.SR), "pipe:1"], capture_output=True, check=True, creationflags=NO_WINDOW).stdout
        with s.lock:
            s.pcm, s.pos = np.frombuffer(raw, np.int16).reshape(-1, 2), 0
            s.ver += 1
        log(f"[音乐] 正在播放：{s.title}")
        threading.Thread(target=s._beats, args=(s.pcm, s.ver, s.title), daemon=True).start()

    def _beats(s, pcm, ver, title):
        """找节拍：响度起伏的自相关定速度，再对齐相位；外加每 0.1 秒的响度。算好发给直播画面，跳舞跟着它。"""
        try:
            sr, hop = 24000, 512
            y = pcm[::2].mean(axis=1).astype(np.float32) / 32768
            n = len(y) // hop
            e = np.sqrt(np.mean(y[:n * hop].reshape(n, hop) ** 2, axis=1))
            on = np.maximum(0, np.diff(e, prepend=e[0]))
            on = on - on.mean()
            ac = np.fft.irfft(np.abs(np.fft.rfft(on, 2 * n)) ** 2)[:n]
            lags = np.arange(int(sr / hop * 60 / 180), int(sr / hop * 60 / 70))
            bpm = 60 * sr / hop / lags
            lag = int(lags[np.argmax(ac[lags] * np.exp(-.5 * (np.log2(bpm / 120) / 1.0) ** 2))])  # 偏向 120 BPM 左右
            phase = int(np.argmax([on[k::lag].sum() for k in range(lag)]))
            bt = np.arange(phase, n, lag) * hop / sr
            step = 2400
            m = len(y) // step
            rms = np.sqrt(np.mean(y[:m * step].reshape(m, step) ** 2, axis=1))
            en = np.clip(rms / (np.percentile(rms, 95) + 1e-6), 0, 1)
        except Exception as e:  # noqa: BLE001
            return log(f"[音乐] 节拍分析失败（跳舞按默认节奏）：{type(e).__name__}: {e}", "warn")
        if ver != s.ver:
            return
        s.beats = {"ver": ver, "title": title, "bpm": round(60 * sr / hop / lag, 1),
                   "beats": [round(float(x), 2) for x in bt], "energy": [round(float(x), 2) for x in en]}
        EV.emit("beats", **s.beats)

    def _next_index(s, step=1):
        """下一首：点歌队列优先（返回那一项，删掉了的跳过），否则按播放方式返回歌单序号。"""
        fs = s.files()
        s.by = ""
        while s.req and step >= 0:
            r = s.req.pop(0)
            if r["path"].exists():
                s.by = r["nick"]
                return r
        n = len(fs) or 1
        if s.mode == "one" and step == 0:
            return s.idx
        if s.mode == "shuffle" and n > 1:
            return random.choice([i for i in range(n) if i != s.idx])
        return (s.idx + max(step, 1) if step >= 0 else s.idx - 1) % n

    def _start(s):
        import sounddevice as sd

        s._stop_streams()
        streams = []
        for k, dev in enumerate(s.e.player.out._targets()):
            st = {"ver": s.ver, "pos": s.pos}

            def cb(out, frames, _t, _status, st=st, main=(k == 0)):
                with s.lock:
                    pcm, ver = s.pcm, s.ver
                if pcm is None or not s.playing:
                    out[:] = 0
                    return
                if st["ver"] != ver:  # 换歌了：从头放
                    st["ver"], st["pos"] = ver, 0
                target = s.vol * (s.duck if s.e.player.busy() else 1.0)
                s.gain += (target - s.gain) * .12 if main else 0  # 每块约 20ms，压低 / 恢复大约 0.2 秒
                chunk = pcm[st["pos"]:st["pos"] + frames]
                out[:len(chunk)] = chunk.astype(np.float32) * (s.gain / 32768)
                out[len(chunk):] = 0
                st["pos"] += len(chunk)
                if main:
                    s.pos = st["pos"]
                    if len(chunk) < frames and not s._ending:  # 放完了：后台解码下一首
                        s._ending = True
                        threading.Thread(target=s._advance, daemon=True).start()

            stream = sd.OutputStream(samplerate=s.SR, channels=2, dtype="float32", device=dev, callback=cb, blocksize=1024)
            stream.start()
            streams.append(stream)
        s.streams = streams

    def _stop_streams(s):
        streams, s.streams = s.streams, []
        for st in streams:
            st.abort()
            st.close()

    def _advance(s):
        try:
            s._load(s._next_index(0))
            s.e.act.on_song(s.title)  # 跳舞：一首放完算一首
        except Exception as e:  # noqa: BLE001
            log(f"[音乐] 下一首加载失败：{e}", "error")
            s.playing = False
        s._ending = False
        s._emit()

    def control(s, action: str, **kw):
        """play / pause / next / prev / select(index) / vol / duck / mode"""
        if action in ("play", "select", "next", "prev"):
            if action == "select":
                s._load(int(kw.get("index", 0)))
            elif action in ("next", "prev"):
                s._load(s._next_index(1 if action == "next" else -1))
            elif s.pcm is None:
                s._load(s.idx)
            s.playing = True
            s._start()
        elif action == "pause":
            s.playing = False
            s._stop_streams()
        elif action == "vol":
            s.vol = max(0.0, min(1.0, float(kw.get("value", s.vol))))
        elif action == "duck":
            s.duck = max(0.0, min(1.0, float(kw.get("value", s.duck))))
        elif action == "mode" and kw.get("value") in s.MODES:
            s.mode = kw["value"]
        s.e.cfg["live"]["music"] = {"vol": s.vol, "duck": s.duck, "mode": s.mode}
        s._emit()

    def restart(s):
        """直播声音设备改了：正在放的话换到新设备上继续。"""
        if s.playing:
            s._start()


# ============================================================ 播放：音频时钟驱动画面，输出 JPEG 给界面

class Segment:
    """一句话：TTS 音频 + 正在生成的帧。"""

    def __init__(s, text, pcm, gen, turn, prev=None):
        s.text, s.pcm, s.gen, s.turn, s.prev = text, pcm, gen, turn, prev
        s.frames, s.done, s.end_idx = [], False, None
        s.words, s.track = [], None  # Live2D / VRM：词时间戳 → 口型时间轴
        s.need = max(1, int(len(pcm) / OUT_SR * FPS))


class Player:
    def __init__(s):
        s.idle = [np.zeros((512, 512, 3), np.uint8)]
        s.i, s.gen, s.pending = 0, 0, deque()
        s.realtime, s.prebuffer = True, 28  # 实时模式攒够 prebuffer 帧就开播；高清模式整句生成完再播
        s.cond, s.jpeg, s.seq = threading.Condition(), None, 0
        s.recent, s.fade, s.speaking = deque(maxlen=8), [], False
        s.gen_fps = 30.0  # 最近的实际生成速度（渲染线程实时更新），用来算开播前要缓冲多少帧
        s.av_offset = 0.0  # 用户微调（秒）：正数 = 画面再晚一点，负数 = 画面早一点
        s.out_latency = 0.0  # 声卡实际输出延迟（秒），每次播放时从音频流读取
        s.param = False  # Live2D / VRM：画面由界面自己画，这里只放声音、发口型时间轴
        s.cur_turn = None  # 正在说的那一轮
        s.out = AudioOut()
        threading.Thread(target=s._loop, daemon=True).start()

    def _publish(s, frame): ...

    def set_idle(s, frames, realtime, prebuffer):
        s.idle, s.realtime, s.prebuffer, s.i, s.fade = frames, realtime, prebuffer, 0, []
        s.recent.clear()  # 换引擎后旧画面尺寸不同，不能再拿来当运动起点

    def interrupt(s):
        s.gen += 1
        s.pending.clear()
        s.out.stop()

    def busy(s):
        return s.speaking or bool(s.pending)

    def buffer_needed(s, need: int) -> int:
        """开播前至少要攒多少帧才不会中途卡：生成按批到达（每批 prebuffer 帧），速度 r 帧/秒、播放 25 帧/秒，
        要保证 b + r·τ − 一批 ≥ 25·τ 对整句成立 → b ≥ 一批 + (25 − r)·时长。r 打 95 折留余量；生成够快时就只等一批。"""
        r = s.gen_fps * 0.95
        return min(need, s.prebuffer + max(0, math.ceil((FPS - r) * need / FPS)))

    def _ready(s, seg):
        return seg.done or (s.realtime and len(seg.frames) >= s.buffer_needed(seg.need))

    def _loop(s):
        t = time.perf_counter()
        while True:
            seg = s.pending[0] if s.pending else None
            if seg is not None and seg.gen != s.gen:
                s.pending.popleft()
                continue
            if seg is not None and s._ready(seg):
                s.pending.popleft()
                s._play(seg)
                t = time.perf_counter()
                continue
            if s.param:  # 没有待机画面要推
                time.sleep(0.02)
                t = time.perf_counter()
                continue
            idle = s.idle[s.i % len(s.idle)]
            if s.fade:  # 回答结束后 8 帧淡回待机画面
                a = (8 - len(s.fade) + 1) / 9
                idle = s.fade.pop(0)
            s._publish(idle)
            s.i += 1
            t += 1 / FPS
            time.sleep(max(0.0, t - time.perf_counter()))

    def _play(s, seg):
        s.speaking, s.cur_turn = True, seg.turn["id"]
        # 声音要经过声卡缓冲才真正出来（实测 MME 扬声器 ~180ms），画面按“听到声音的时刻”对齐，否则嘴型会抢在声音前面；
        # 选了直播声音设备时按它的延迟对齐（观众听到的是这一路）
        s.out_latency = s.out.play(seg.pcm)
        if s.on_start:
            try:
                s.on_start(seg)
            except Exception as e:  # noqa: BLE001
                log(f"[场景] 指令出错：{e}", "warn")
        t0, last, late = time.perf_counter(), -1, 0
        delay = s.out_latency + s.av_offset
        EV.emit("play", turn=seg.turn["id"], text=seg.text, latency=t0 - seg.turn["t0"] if seg.prev is None else None)
        if seg.track:  # 界面按同一台机器的时钟对齐：start 是“听到声音”的时刻
            EV.emit("speak", turn=seg.turn["id"], start=time.time() + delay, **seg.track)
        dur = len(seg.pcm) / OUT_SR
        while (el := time.perf_counter() - t0) < dur + max(0.0, delay):
            if seg.gen != s.gen:
                s.out.stop()
                break
            if seg.track:
                time.sleep(0.02)
                continue
            k = min(max(0, int((el - delay) * FPS)), seg.need - 1)
            if k != last:
                if k >= len(seg.frames):
                    late += 1  # 生成跟不上，停在最后一帧
                s._publish(seg.frames[min(k, len(seg.frames) - 1)])
                last = k
            time.sleep(0.008)
        if late:
            log(f"[播放] 生成跟不上播放，卡顿 {late} 帧（当前生成 {s.gen_fps:.1f} fps）", "warn")
        s.speaking = False
        EV.emit("play_end", turn=seg.turn["id"])
        if not s.pending and not seg.track:
            if seg.end_idx is not None:  # MuseTalk：待机循环从说完的那一帧接着播，完全无缝
                s.i = seg.end_idx
            else:  # FlashHead：说话画面和待机画面不是同一段，淡入衔接
                s.fade = [seg.frames[min(seg.need, len(seg.frames)) - 1]] * 8


# ============================================================ 总控

class Danmaku:
    """直播弹幕：去重、过滤、排队，掌柜说完一轮再回答下一条（不打断自己）；提问优先，等太久的放弃；
    进场的人攒一会儿，空闲时合起来欢迎一次。来源：抖音直播间网页（main.open_danmaku_window）、控制台模拟留言、/api/danmaku_push。"""

    QUESTION = re.compile(r"[?？]|吗|呢|多少|几点|几岁|几号|怎么|如何|能不能|可不可以|可以吗|有没有|哪|什么|啥|价格|门票|票价|营业|地址|停车|生日|团购|优惠")
    ADS = re.compile(r"https?://|www\.|\.com|微信|vx|v信|威信|加我|私聊|扣扣|qq|\d{7,}", re.I)
    KOU = re.compile(r"^\s*(\d)\1*\s*$")  # 扣 1 / 22 / 1111：回应主播的互动，不逐条回答，攒起来统一回应
    KOU_SAY = ["看到好多朋友扣{d}啦，谢谢大家！", "扣{d}的朋友我都看到啦，安排上！", "哇，一大波{d}刷起来了，谢谢支持～"]
    WELCOME = ["欢迎{names}来到直播间！有什么想问的直接发弹幕问我哦～", "{names}来啦，欢迎欢迎！想了解门票、活动都可以问我～",
               "欢迎{names}！刚进来的朋友可以先看看黑板，有问题随时发弹幕～", "哈喽{names}，欢迎来玩！今天想问点什么呀？"]
    WELCOME_CHAT = ["欢迎{names}来到直播间！随便坐，想聊什么直接发弹幕～", "{names}来啦，欢迎欢迎！今天过得怎么样？",
                    "哈喽{names}，欢迎欢迎，一起唠会儿嗑吧～", "欢迎{names}！刚好在聊天，快来一起～"]
    NICK_JUNK = re.compile(r"[^\w一-鿿]+")  # 昵称里的表情、符号念出来很怪，欢迎时去掉
    SONG = re.compile(r"^\s*(?:点歌|点一首|我要听|来一首)\s*[:：]?\s*《?(.{1,30}?)》?\s*$")
    THANKS = ["爱你们～", "太感动了！", "收到啦，比心！", "今天的 token 有着落啦～"]
    INSTANT_FRESH = 20  # 即时欢迎：进来超过这么多秒还没轮到的就不欢迎了（多半已经划走）

    def __init__(s, engine):
        s.e, s.q, s.lock = engine, [], threading.Lock()
        s.recent = deque(maxlen=60)  # 控制台显示的最近弹幕及处理结果
        s.seen = {}  # (昵称, 内容) → 时间，去重用
        s.texts = {}  # 内容 → 时间，刷屏判断用
        s.enter, s.last_welcome, s.last_busy = [], 0.0, time.time()
        s.songsay = []  # 点歌的回话（固定话术，空下来马上说）
        s.thanks = []  # 刚送礼物的 (昵称, 礼物, 时间)，空下来马上道谢（固定话术，一定会谢）
        s.nid = 0  # 留言编号
        s.kou, s.last_kou = {}, 0.0  # 扣的数字 → 这一波扣的人
        s.last_comment, s.last_cold, s.cold_used = time.time(), time.time(), []  # 唠嗑冷场：最后一条留言、上次主动开聊、用过的话题
        s.stat = {"comments": 0, "answered": 0, "skipped": 0, "enters": 0, "likes": 0, "gifts": 0}
        s.source = {"state": "off", "title": "", "t": 0}  # 网页弹幕窗口的状态（main 更新）
        threading.Thread(target=s._loop, daemon=True).start()

    def _note(s, item, status):
        if "id" not in item:
            s.nid += 1
            item["id"] = s.nid
        item["status"] = status
        EV.emit("danmaku", item=item, stat=s.stat)

    @staticmethod
    def _bare(t: str) -> str:
        return re.sub(r"[\s\W_]+", "", t.lower())

    def blocked(s, d: dict, *texts: str) -> str:
        """昵称 / 内容里有屏蔽词就返回那个词（中间夹空格、符号、大小写不同也算，防“加 微 信”这种）。"""
        words = [s._bare(w) for w in d.get("block", []) if s._bare(w)]
        hay = " ".join(s._bare(t or "") for t in texts)
        return next((w for w in words if w in hay), "")

    def _skip(s, d: dict, nick: str, text: str, now: float):
        if not text or not re.search(r"[\w\u4e00-\u9fff]", text):
            return "只有表情"
        if s.ADS.search(text):
            return "疑似广告"
        if now - s.seen.get((nick, text), 0) < 120:
            return "重复"
        if now - s.texts.get(text, 0) < 30 and len(text) <= 8:
            return "刷屏"
        return ""

    def push(s, items: list):
        """items: [{type: comment|enter|like, nick, text}]"""
        d, now = s.e.cfg["live"]["danmaku"], time.time()
        with s.lock:
            for it in items[:50]:
                kind, nick, text = it.get("type", "comment"), str(it.get("nick", ""))[:30].strip(), str(it.get("text", ""))[:120].strip()
                if kind == "like":
                    s.stat["likes"] += 1
                    continue
                bw = s.blocked(d, nick, text) if kind != "other" else ""
                if bw:  # 屏蔽词：评论、假礼物、进场、礼物道谢、点歌，一律不理（真礼物照样记账）
                    if kind == "gift":
                        s.e.ledger.gift(text[:20] or "礼物", max(1, min(9999, int(it.get("n") or 1))))
                    if kind != "enter":
                        o = {"t": now, "nick": nick or "观众", "text": text if kind != "gift" else f"送出了 {text}"}
                        s.recent.append(o)
                        s.stat["skipped"] += 1
                        s._note(o, f"屏蔽词「{bw}」：忽略")
                    continue
                if kind == "fakegift":  # 观众打字冒充的礼物：不记账、不道谢；空下来时让掌柜开个玩笑接住（不当真）
                    s.stat["fake"] = s.stat.get("fake", 0) + 1
                    o = {"t": now, "nick": nick or "观众", "text": text, **({"test": True} if it.get("test") else {})}
                    s.recent.append(o)
                    if s.e.cfg["live"]["auto"] and not any(x.get("fake") and x["nick"] == o["nick"] for x in s.q):  # 同一个人连刷只接一次
                        o.update(pri=3, fake=True)
                        s.q.append(o)
                        s._note(o, "假礼物：排队开玩笑")
                    else:
                        s._note(o, "假礼物：" + str(it.get("why", ""))[:20])
                    continue
                if kind == "other":  # 认不出的消息（可能是礼物、关注、分享……）：只在控制台列出来，方便对照网页改识别规则
                    o = {"t": now, "nick": "（网页消息）", "text": text}
                    s.recent.append(o)
                    s._note(o, "未识别")
                    continue
                if kind == "gift":  # text = 礼物名，n = 个数
                    n = max(1, min(9999, int(it.get("n") or 1)))
                    s.stat["gifts"] = s.stat.get("gifts", 0) + n
                    s.e.ledger.gift(text[:20] or "礼物", n)
                    s.e.act.on_gift(text[:20], n, s.NICK_JUNK.sub("", nick)[:12])  # 礼物心愿
                    g = {"t": now, "nick": nick, "text": f"送出了 {text[:20] or '礼物'} ×{n}"}
                    s.recent.append(g)
                    s._note(g, "礼物：已记账")
                    name = s.NICK_JUNK.sub("", nick)[:12]
                    if name and name not in [x for x, _, _ in s.thanks]:
                        s.thanks = (s.thanks + [(name, text[:20], now)])[-4:]
                    continue
                if kind == "enter":
                    s.stat["enters"] += 1
                    nick = s.NICK_JUNK.sub("", nick or "")[:12]
                    if nick and nick not in [n for n, _ in s.enter]:
                        s.enter = (s.enter + [(nick, now)])[-5:]
                    continue
                s.stat["comments"] += 1
                s.last_comment = now
                item = {"t": now, "nick": nick or "观众", "text": text, **({"test": True} if it.get("test") else {})}
                s.recent.append(item)
                st = s.e.act.on_comment(item["nick"], text)  # 小游戏的答案（数字 / ABCD）：收下记分，不排队回答
                if st:
                    s._note(item, st)
                    continue
                m = s.KOU.match(text)
                if m:  # 扣数字：记下来，够多人了空闲时统一回应一句
                    s.kou.setdefault(m.group(1), set()).add(item["nick"])
                    s._note(item, f"互动扣{m.group(1)}：已有 {len(s.kou[m.group(1)])} 人，满 3 人统一回应")
                    continue
                why = s._skip(d, item["nick"], text, now)
                s.seen[(item["nick"], text)], s.texts[text] = now, now
                if why:
                    s.stat["skipped"] += 1
                    s._note(item, "跳过：" + why)
                    continue
                if not s.e.cfg["live"]["auto"]:  # 人工接管：只记进聊天记录
                    s.e.chat(text, "danmaku", name=item["nick"], test=item.get("test", False))
                    s._note(item, "已记录（人工接管中）")
                    continue
                song = s.SONG.match(text) if d.get("song", True) else None
                if song:
                    who = s.NICK_JUNK.sub("", item["nick"])[:12] or "这位朋友"
                    done = lambda r, who=who, item=item: (s.songsay.append(s._song_reply(r, who)),
                                                          s._note(item, "点歌：" + (f"《{r['title']}》" if r.get("ok") else "没找到")))
                    r = s.e.music.request(song.group(1), item["nick"], d.get("song_cut", True), d.get("song_online", True), done)
                    if r.get("searching"):
                        s._note(item, "点歌：网上找歌中…")
                    else:
                        done(r)
                    continue
                item["pri"] = 2 if s.QUESTION.search(text) else 0 if len(text) <= 2 else 1
                s.q.append(item)
                s._note(item, "排队中")
            if len(s.seen) > 2000:  # 清理过期的去重记录
                s.seen = {k: t for k, t in s.seen.items() if now - t < 300}
                s.texts = {k: t for k, t in s.texts.items() if now - t < 300}

    @staticmethod
    def _song_reply(r: dict, who: str) -> str:
        title = r.get("title", "").split(" - ")[-1]  # 「歌手 - 歌名」只念歌名
        return (f"收到{who}点的《{title}》，马上安排！" if r.get("now") else f"《{title}》已经在歌单里排着啦～" if r.get("dup") else
                f"{who}点的《{title}》排在第 {r['pos']} 首～" if r.get("ok") else "点歌的朋友有点多，等前面几首放完再点哦～" if r.get("full") else
                f"《{r['query']}》网上没找到能放的版本，可能要会员，换一首试试？" if r.get("online") else f"歌单里暂时没有《{r['query']}》，换一首试试？")

    def busy(s) -> bool:
        return s.e.replying > 0 or s.e.player.busy()


    def _answer_next(s, pre: bool = False) -> bool:
        """从队里取下一批交给掌柜：清掉等太久的；积压（≥2 条）时几条合成一轮一起回；只剩这一条但后面还在来时让这次说短点。返回有没有取到。"""
        now, d = time.time(), s.e.cfg["live"]["danmaku"]
        with s.lock:
            for it in [x for x in s.q if now - x["t"] > d.get("max_wait", 90)]:  # 等太久的不回了
                s.q.remove(it)
                s.stat["skipped"] += 1
                s._note(it, "跳过：等太久")
            if len(s.q) > 4:  # 积压时只留提问和有内容的
                for it in [x for x in s.q if x["pri"] == 0]:
                    s.q.remove(it)
                    s.stat["skipped"] += 1
                    s._note(it, "跳过：太多留言")
            if not s.q or not s.e.cfg["live"]["auto"]:
                return False
            fakes = [x for x in s.q if x.get("fake")]
            if fakes:  # 假礼物排最前、单独一轮：几个人一起刷就一起接，开玩笑不道谢
                for x in fakes[:3]:
                    s.q.remove(x)
                take, left = fakes[:3], len(s.q)
        if fakes:
            names = "、".join(x["nick"] for x in take)
            for x in take:
                s._note(x, "假礼物：开玩笑接住" + ("·预备" if pre else ""))
            s.e.chat("；".join(x["text"] for x in take), "danmaku", name=names, pre=pre, brief=left > 0, test=all(x.get("test") for x in take), hint=(
                f"（注意：「{names}」只是在弹幕里打字假装送礼物，并没有真的送。不要道谢，不要说“收到”“谢谢礼物”，"
                "也别板着脸揭穿或批评；用轻松幽默的方式接个梗、开个玩笑，比如假装被这份“空气礼物”感动、或者打趣一句，一两句就好。）"))
            return True
        with s.lock:
            order = sorted(s.q, key=lambda x: (-x["pri"], x["t"]))  # 提问优先，同级先来先回
            if not order:
                return False
            at, most = int(d.get("batch_at", 2)), int(d.get("batch_max", 4))  # 控制台「合并回答」：排到几条起合并（0 = 不合并）、一次最多几条
            take = order[:most] if at and len(s.q) >= at else order[:1]
            for it in take:
                s.q.remove(it)
            left = len(s.q)
        batch = {}  # 同一个人连发几条：合成一条
        for it in take:
            batch[it["nick"]] = (batch[it["nick"]] + "；" + it["text"]) if it["nick"] in batch else it["text"]
        for it in take:
            s.stat["answered"] += 1
            s._note(it, ("已回答" if len(batch) == 1 else f"合并回答（{len(batch)} 人）") + ("·预备" if pre else ""))
        test = all(x.get("test") for x in take)  # 全是控制台测试留言：这轮不进对话记录和语料
        if len(batch) == 1:
            nick, text = next(iter(batch.items()))
            s.e.chat(text, "danmaku", name=nick, pre=pre, brief=left > 0, test=test)
        else:
            s.e.chat("；".join(f"{n}：{t}" for n, t in batch.items()), "danmaku", pre=pre, batch=list(batch.items()), test=test)
        return True

    def _prefetch(s):
        """掌柜还在说上一条时，把下一条提前交给大模型、合成好排在后面：上一条一说完就接上，不再停顿几秒等大模型。
        只提前一条（播放队列里除了正在说的这轮没有别的才做）；礼物道谢、点歌回话要插空说，有它们时不提前。"""
        p = s.e.player
        if (s.e.replying or not s.q or s.songsay or s.thanks or s.e.act.say_q or not s.e.cfg["live"]["auto"]
                or any(seg.turn["id"] != p.cur_turn for seg in list(p.pending))):
            return
        s._answer_next(pre=True)

    def _loop(s):
        while True:
            time.sleep(.4)
            try:
                s._tick()
            except Exception as e:  # noqa: BLE001
                log(f"[弹幕] 出错：{type(e).__name__}: {e}", "error")

    def _tick(s):
        now = time.time()
        if not (s.e.tts and (s.e.llm or not s.e.cfg["llm"].get("api_key"))):  # 语音和大模型还在加载：留言先排着，别发出去丢了
            return
        if s.busy():
            s.last_busy = now
            s._prefetch()
            return
        if now - s.last_busy < .3:
            return
        d = s.e.cfg["live"]["danmaku"]
        if s.songsay and s.e.cfg["live"]["auto"]:  # 点歌回话：只说最新的一句，前面的合掉
            say, s.songsay = s.songsay[-1], []
            s.e.say(say)
            return
        if d.get("thanks", True) and s.thanks and s.e.cfg["live"]["auto"]:  # 礼物道谢排最前：固定话术，不经过大模型
            fresh, s.thanks = [x for x in s.thanks if now - x[2] <= 60], []
            if fresh:
                gift = fresh[-1][1] or "礼物"
                s.e.say(f"谢谢{'、'.join(n for n, _, _ in fresh[-3:])}送的{gift}！" + random.choice(s.THANKS))
                return
        if s.e.cfg["live"]["auto"] and s.e.act.speak_next():  # 活动的串场话（游戏出题、揭晓、跳舞热场）
            return
        if d.get("welcome", True) and d.get("welcome_mode") == "instant" and s.e.cfg["live"]["auto"]:
            fresh = [n for n, t in s.enter if now - t <= s.INSTANT_FRESH]
            s.enter = []
            if fresh:  # 进来直接欢迎：固定欢迎词，不经过大模型，也不等空闲间隔
                s.last_welcome = now
                s.e.say((d.get("welcome_text") or "欢迎{names}来到直播间～").replace("{names}", "、".join(fresh[-3:])))
                return
        elif now - s.last_busy < 1.2:  # 说完停一下再接下一条，听起来不赶
            return
        if s._answer_next():
            return
        top = max(s.kou.items(), key=lambda kv: len(kv[1]), default=None)
        if top and len(top[1]) >= 3 and now - s.last_kou >= 45 and s.e.cfg["live"]["auto"]:
            s.kou, s.last_kou = {}, now
            s.e.say(random.choice(s.KOU_SAY).format(d=top[0]))
            return
        live = s.e.cfg["live"]
        cold = live.get("cold") or {}
        if live.get("mode") == "chat" and live["auto"] and cold.get("on", True) and not s.q and s.e.act.id == "talk" \
                and now - max(s.last_comment, s.last_busy, s.last_cold) >= cold.get("gap", 45):
            topics = [t for t in (s.e.persona.get("topics") or ["最近有什么开心的事", "周末打算干嘛", "最近吃了什么好吃的"]) if t not in s.cold_used]
            if not topics:
                s.cold_used, topics = [], list(s.e.persona.get("topics") or ["最近有什么开心的事"])
            topic = random.choice(topics)
            s.cold_used.append(topic)
            s.last_cold = now
            s.e.cold_talk(topic)
            return
        if d.get("welcome", True) and s.enter and now - s.last_welcome >= d.get("welcome_gap", 60) and s.e.cfg["live"]["auto"]:
            names, s.enter = "、".join(n for n, _ in s.enter[-3:]), []
            s.last_welcome = now
            s.e.say(random.choice(s.WELCOME_CHAT if live.get("mode") == "chat" else s.WELCOME).format(names=names))

    def view(s) -> dict:
        return {"recent": list(s.recent)[-30:], "stat": s.stat, "queue": len(s.q), "source": s.source}

    def op(s, op: str, ids: list) -> dict:
        """控制台管理：skip = 忽略（不回答，留在列表里标「已忽略」），delete = 从列表里删掉（排队的也不回答了）；ids 为空 = 全部排队中的。"""
        with s.lock:
            hit = [x for x in list(s.recent) if x.get("id") in ids] if ids else [x for x in s.q]
            for x in hit:
                if x in s.q:
                    s.q.remove(x)
                    s.stat["skipped"] += 1
                if op == "delete":
                    try:
                        s.recent.remove(x)
                    except ValueError:
                        pass
                else:
                    s._note(x, "已忽略（手动）")
        return s.view()


class Showcase:
    """唠嗑版没有店铺展示：保留接口，什么也不切。"""

    def __init__(s, engine):
        s.e, s.id, s.picks, s.pending, s.after, s.log, s.focus = engine, "main", {}, None, [], [], ""

    @staticmethod
    def split(text: str) -> tuple[list, str]:
        return [], re.sub(r"[〔【][^〔〕【】]{0,160}[〕】]", "", text)

    def plan(s, text: str, batch: bool = False) -> dict:
        return {"kind": "none", "show": "", "turn": {}, "hints": []}

    def cats(s) -> list:
        return []

    def manual(s, sid: str, pick: dict | None = None) -> dict:
        return {"id": "main"}

    def view(s) -> dict:
        return {"id": "main", "log": [], "picks": {}}

    def link(s, *a): ...

    def on_segment(s, seg): ...

    def pace(s, turn): ...

    def check(s): ...


class Ledger:
    """token 账本 data/ledger.json：按天记大模型用量（接口返回的 usage，分缓存命中 / 未命中 / 输出）和抖音礼物（名字 → 个数）；
    DeepSeek 的账户余额走官方接口 GET /user/balance（5 分钟查一次）。换算成钱在直播画面的「token 账本」组件里按它的单价算。"""

    def __init__(s, engine, path: Path):
        s.e, s.path, s.lock, s.last_emit, s.bal_t = engine, path, threading.RLock(), 0.0, 0.0  # RLock：保存时在锁里取 view
        s.data = read_json(path, {"days": {}})
        s.balance = None

    def _today(s) -> dict:
        lt = time.localtime()
        return s.data["days"].setdefault(f"{lt.tm_year}-{lt.tm_mon:02d}-{lt.tm_mday:02d}",
                                         {"hit": 0, "miss": 0, "out": 0, "calls": 0, "gifts": {}, "givers": 0})

    def _save(s):
        s.data["days"] = dict(list(s.data["days"].items())[-90:])  # 只留最近 90 天
        save_text(s.path, json.dumps(s.data, ensure_ascii=False))
        if time.time() - s.last_emit > 1:
            s.last_emit = time.time()
            EV.emit("ledger", **s.view())

    # DeepSeek 官方价格（元 / 百万 token：缓存命中输入、未命中输入、输出），高峰价；空闲时段（工作日 9–12、14–18 点以外和周末）半价。
    # 2026-10 官网：deepseek-chat 现在由 deepseek-flash 提供。别家模型不在表里：按「token 账本」组件里填的单价算
    PRICES = {"deepseek-flash": (0.04, 2, 8), "deepseek-v4-flash": (0.04, 2, 8), "deepseek-v4-pro": (0.30, 9, 27)}

    @staticmethod
    def peak(t: float | None = None) -> bool:
        lt = time.localtime(t)
        return lt.tm_wday < 5 and (9 <= lt.tm_hour < 12 or 14 <= lt.tm_hour < 18)

    def price(s, model: str) -> tuple | None:
        m = (model or "").lower()
        base = next((v for k, v in s.PRICES.items() if m.startswith(k)), None)
        return tuple(x * (1 if s.peak() else .5) for x in base) if base else None

    def clear(s, scope: str = "today"):
        """清空账本：today = 今天的用量和礼物，all = 全部。"""
        with s.lock:
            if scope == "all":
                s.data["days"] = {}
            else:
                lt = time.localtime()
                s.data["days"].pop(f"{lt.tm_year}-{lt.tm_mon:02d}-{lt.tm_mday:02d}", None)
            s.last_emit = 0
            s._save()
        return s.view()

    def add(s, u, model: str = ""):
        """一轮回答的 usage。DeepSeek 分 prompt_cache_hit / miss；别家只有 prompt_tokens 和 cached_tokens。
        按这一次调用实际服务的模型和当时的时段算钱（cost，元），累加进当天；认不出的模型不算钱（cost 不动，组件按自己填的单价算）。"""
        hit = getattr(u, "prompt_cache_hit_tokens", None)
        if hit is None:
            hit = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        miss = getattr(u, "prompt_cache_miss_tokens", None)
        miss = (u.prompt_tokens or 0) - hit if miss is None else miss
        with s.lock:
            d = s._today()
            d["hit"] += hit; d["miss"] += miss; d["out"] += u.completion_tokens or 0; d["calls"] += 1
            pr = s.price(model)
            if pr:
                d["cost"] = d.get("cost", 0.0) + (hit * pr[0] + miss * pr[1] + (u.completion_tokens or 0) * pr[2]) / 1e6
                d["priced"] = d.get("priced", 0) + 1
                d["model"] = model
            s._save()
        s.refresh_balance()

    def gift(s, name: str, n: int):
        with s.lock:
            g = s._today()
            g["gifts"][name] = g["gifts"].get(name, 0) + n
            g["givers"] += 1
            s._save()

    def refresh_balance(s, force=False):
        """DeepSeek 官方余额接口；别的服务商没有统一的余额接口，不查。"""
        c = s.e.cfg["llm"]
        if "deepseek.com" not in c.get("base_url", "") or not c.get("api_key") or (not force and time.time() - s.bal_t < 300):
            return
        s.bal_t = time.time()

        def run():
            try:
                import httpx
                r = httpx.get(c["base_url"].rstrip("/").removesuffix("/v1") + "/user/balance",
                              headers={"Authorization": f"Bearer {c['api_key']}"}, timeout=15).json()
                info = next((b for b in r.get("balance_infos", []) if b.get("currency") == "CNY"), (r.get("balance_infos") or [None])[0])
                if info:
                    s.balance = {"total": float(info["total_balance"]), "currency": info.get("currency", "CNY"), "ok": r.get("is_available", True)}
                    EV.emit("ledger", **s.view())
            except Exception as e:  # noqa: BLE001
                log(f"[账本] 查余额失败：{type(e).__name__}", "warn")
        threading.Thread(target=run, daemon=True).start()

    def view(s) -> dict:
        with s.lock:
            return {"today": dict(s._today()), "balance": s.balance}


class Corpus:
    """语料库 data/corpus.db（SQLite）：每轮对话原样存下，留着以后后训练本地模型。
    存的是模型这一轮实际看到的全部内容——系统提示词（按哈希只存一份）、上下文、带时间前缀的问题——和它的回答；
    人工可以打分（好 / 差）、把回答改写成“理想回答”。导出时：理想回答或好评回答 → SFT，原回答 vs 改写 → DPO 偏好对。
    kind：llm = 大模型回答；script = 人工代说 / 欢迎 / 扣数字等固定话术；legacy = 从旧的聊天记录导入（没有上下文）。"""

    FIELDS = "id, t, kind, source, name, question, reply, complete, model, shop, latency, rating, ideal, note"

    def __init__(s, path: Path, history: list):
        import sqlite3

        path.parent.mkdir(parents=True, exist_ok=True)
        s.db, s.lock = sqlite3.connect(str(path), check_same_thread=False), threading.Lock()
        s.db.executescript("""
            CREATE TABLE IF NOT EXISTS prompts (hash TEXT PRIMARY KEY, text TEXT);
            CREATE TABLE IF NOT EXISTS turns (id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, kind TEXT, source TEXT, name TEXT,
              question TEXT, ask TEXT, context TEXT, prompt TEXT, reply TEXT, complete INTEGER, model TEXT, shop TEXT, latency REAL,
              rating INTEGER DEFAULT 0, ideal TEXT DEFAULT '', note TEXT DEFAULT '');
            CREATE INDEX IF NOT EXISTS turns_t ON turns(t);
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);""")
        if not s.db.execute("SELECT 1 FROM meta WHERE k='legacy'").fetchone():  # 第一次：把旧聊天记录导进来
            s._import_legacy(history)
            s.db.execute("INSERT INTO meta VALUES ('legacy', '1')")
        s.db.commit()

    def _import_legacy(s, history: list):
        rows, q = [], None
        for m in history:
            if m["role"] == "user":
                q = m
            elif q and m.get("turn") == q.get("turn"):
                rows.append((m.get("t", q.get("t", 0)), "legacy", q.get("source", "text"), q.get("name", ""), q["text"], q["text"], "[]", "",
                             m["text"], 1, "", "", None))
                q = None
        s.db.executemany("INSERT INTO turns (t, kind, source, name, question, ask, context, prompt, reply, complete, model, shop, latency)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        log(f"[语料] 已导入旧聊天记录 {len(rows)} 轮")

    def add(s, kind: str, source: str, name: str, question: str, reply: str, complete: bool, msgs: list | None = None,
            model: str = "", shop: str = "", latency: float | None = None):
        msgs = msgs or []
        system = msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else ""
        h = __import__("hashlib").sha1(system.encode()).hexdigest()[:16] if system else ""
        with s.lock:
            if h:
                s.db.execute("INSERT OR IGNORE INTO prompts VALUES (?, ?)", (h, system))
            s.db.execute("INSERT INTO turns (t, kind, source, name, question, ask, context, prompt, reply, complete, model, shop, latency)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (time.time(), kind, source, name or "", question, msgs[-1]["content"] if msgs else question,
                          json.dumps(msgs[1:-1] if system else msgs[:-1], ensure_ascii=False), h, reply, int(complete), model, shop, latency))
            s.db.commit()

    def _where(s, f: dict):
        sql, args = [], []
        if f.get("q"):
            sql.append("(question LIKE ? OR reply LIKE ? OR ideal LIKE ? OR name LIKE ?)")
            args += [f"%{f['q']}%"] * 4
        if f.get("kind"):
            sql.append("kind = ?"); args.append(f["kind"])
        if f.get("source"):
            sql.append("source = ?"); args.append(f["source"])
        if f.get("rating") in ("1", "-1", "0", 1, -1, 0):
            sql.append("rating = ?"); args.append(int(f["rating"]))
        if f.get("edited"):
            sql.append("ideal != ''")
        if f.get("from"):
            sql.append("t >= ?"); args.append(time.mktime(time.strptime(f["from"], "%Y-%m-%d")))
        if f.get("to"):
            sql.append("t < ?"); args.append(time.mktime(time.strptime(f["to"], "%Y-%m-%d")) + 86400)
        return (" WHERE " + " AND ".join(sql)) if sql else "", args

    def query(s, f: dict, page: int = 0, size: int = 50) -> dict:
        where, args = s._where(f)
        with s.lock:
            total = s.db.execute(f"SELECT COUNT(*) FROM turns{where}", args).fetchone()[0]
            rows = s.db.execute(f"SELECT {s.FIELDS} FROM turns{where} ORDER BY t DESC LIMIT ? OFFSET ?", args + [size, page * size]).fetchall()
            st = s.db.execute("SELECT COUNT(*), SUM(rating=1), SUM(rating=-1), SUM(ideal!=''), SUM(kind='llm') FROM turns").fetchone()
        keys = [k.strip() for k in s.FIELDS.split(",")]
        return {"rows": [dict(zip(keys, r)) for r in rows], "total": total,
                "stats": dict(zip(("all", "good", "bad", "edited", "llm"), [x or 0 for x in st]))}

    def item(s, i: int) -> dict:
        with s.lock:
            r = s.db.execute("SELECT ask, context, prompt FROM turns WHERE id = ?", (i,)).fetchone()
            sysp = s.db.execute("SELECT text FROM prompts WHERE hash = ?", (r[2],)).fetchone() if r and r[2] else None
        return {"ask": r[0], "context": json.loads(r[1] or "[]"), "system": sysp[0] if sysp else ""} if r else {}

    def update(s, ids: list, **kw):
        cols = {k: v for k, v in kw.items() if k in ("rating", "ideal", "note")}
        if not cols or not ids:
            return
        with s.lock:
            s.db.executemany(f"UPDATE turns SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?", [(*cols.values(), int(i)) for i in ids])
            s.db.commit()

    def delete(s, ids: list):
        with s.lock:
            s.db.executemany("DELETE FROM turns WHERE id = ?", [(int(i),) for i in ids])
            s.db.commit()

    def export(s, f: dict, fmt: str, out_dir: Path, anon=True, good_only=True) -> dict:
        """fmt：openai（{"messages": [...]}）/ sharegpt（{"conversations": [...]}）/ alpaca / dpo（chosen = 理想回答，rejected = 原回答）。
        good_only：只要好评或改写过的（差评且没改写的不要）；anon：观众昵称换成「观众1」「观众2」。"""
        where, args = s._where({**f, "kind": f.get("kind") or "llm"})
        with s.lock:
            rows = s.db.execute(f"SELECT name, ask, context, reply, ideal, rating, complete, p.text FROM turns"
                                f" LEFT JOIN prompts p ON p.hash = turns.prompt{where} ORDER BY turns.t", args).fetchall()
        names: dict = {}

        def anonymize(text: str, extra=()) -> str:
            if not anon:
                return text
            found = re.findall(r"观众「([^」]+)」", text)  # 上下文里别的观众：发给模型的消息都带着「直播间观众「昵称」发来弹幕」
            for n in sorted({*names, *extra, *found}, key=len, reverse=True):
                if n:
                    names.setdefault(n, f"观众{len(names) + 1}")
                    text = text.replace(n, names[n])
            return text

        out, n = [], 0
        for name, ask, ctx, reply, ideal, rating, complete, system in rows:
            if fmt == "dpo" and not ideal:
                continue
            if fmt != "dpo" and (not complete and not ideal or good_only and rating < 0 and not ideal):
                continue
            ctx = json.loads(ctx or "[]")
            answer = ideal or reply
            ask, ctx = anonymize(ask, [name]), [{**m, "content": anonymize(m["content"])} for m in ctx]
            msgs = ([{"role": "system", "content": system}] if system else []) + ctx + [{"role": "user", "content": ask}]
            if fmt == "openai":
                rec = {"messages": msgs + [{"role": "assistant", "content": answer}]}
            elif fmt == "sharegpt":
                rec = {"conversations": [{"from": {"system": "system", "user": "human", "assistant": "gpt"}[m["role"]], "value": m["content"]}
                                         for m in msgs + [{"role": "assistant", "content": answer}]]}
            elif fmt == "alpaca":
                pairs = [[ctx[i]["content"], ctx[i + 1]["content"]] for i in range(0, len(ctx) - 1, 2)
                         if ctx[i]["role"] == "user" and ctx[i + 1]["role"] == "assistant"]
                rec = {"system": system or "", "instruction": ask, "input": "", "output": answer, "history": pairs}
            else:
                rec = {"messages": msgs, "chosen": ideal, "rejected": reply}
            out.append(json.dumps(rec, ensure_ascii=False))
            n += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"语料-{fmt}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
        log(f"[语料] 已导出 {n} 条（{fmt}）到 {path.name}")
        return {"path": str(path), "count": n}


class Activity:
    """唠嗑直播的「活动」：唠嗑（默认）/ 舞台跳舞（跟着背景音乐的节拍跳，跳几首自动回来）/ 弹幕小游戏（猜数字、答题挑战，主播当主持人，
    程序判对错记分，弹幕里的答案不进回答队列）。礼物心愿在后台一直计数：攒够了主播道谢并兑现（如跳一支舞）。
    主播的串场话放进 say_q，由弹幕那边空闲时说（不打断正在说的话）。设置在 cfg.live.activity，运行状态在这里，变了发 SSE「activity」。"""

    NAMES = {"talk": "唠嗑", "dance": "舞台跳舞", "game": "弹幕小游戏"}
    GAMES = {"guess": "猜数字", "quiz": "答题挑战"}
    DEFAULT = {"dance": {"songs": 1, "power": 1.0}, "game": {"type": "guess", "rounds": 5, "secs": 20, "topic": "", "level": "mid"},
               "wish": {"on": False, "items": [{"gift": "小心心", "n": 99, "reward": "跳一支舞", "act": "dance"}]}}
    LEVELS = {"easy": "轻松：大多数人都能答对，但不要幼儿园水平。",
              "mid": "中等：大约一半的人能答对。不要出“世界上最大的动物”“熊猫吃什么”这类人人都知道的题，要让人想一想。",
              "hard": "困难：只有两三成的人能答对，需要真正知道这个知识才行，但答案必须确定、查得到。"}
    QUIZ_BANK = [  # 没配大模型时的备用题
        {"q": "一年中白天最长的是哪个节气？", "opts": ["春分", "夏至", "秋分", "冬至"], "ans": 1, "why": "夏至太阳直射北回归线"},
        {"q": "熊猫的主食是什么？", "opts": ["竹子", "苹果", "鱼", "蜂蜜"], "ans": 0, "why": "大熊猫九成以上吃竹子"},
        {"q": "彩虹一共有几种颜色？", "opts": ["五种", "六种", "七种", "八种"], "ans": 2, "why": "赤橙黄绿青蓝紫"},
        {"q": "地球上最大的海洋是？", "opts": ["大西洋", "印度洋", "北冰洋", "太平洋"], "ans": 3, "why": "太平洋面积最大"},
        {"q": "一只蜘蛛有几条腿？", "opts": ["六条", "八条", "十条", "十二条"], "ans": 1, "why": "蜘蛛是八条腿"},
    ]

    def __init__(s, engine):
        s.e, s.id, s.lock = engine, "talk", threading.RLock()
        s.g = None  # 游戏这一局的状态
        s.scores = {}  # 昵称 → 分（换游戏、点「清零」才清）
        s.got = {}  # 礼物心愿：礼物名 → 这一轮攒到的个数
        s.say_q = []  # 待说的串场话 [{"text"} 或 {"ask"}（交给大模型说）, 可带 "on"：开口时回调]
        s.songs, s.back = 0, "talk"
        s.quiz_q, s.quiz_key = [], None  # 备好的题（一次出一批）；是哪个类别和难度的
        s.asked = read_json(engine.data / "quiz_asked.json", None)  # 考过的题，第一次从以前的对局记录里取
        if s.asked is None:
            s.asked = [{"q": r["q"], "point": ""} for x in read_json(engine.data / "games.json", []) for r in x.get("rounds", []) if r.get("q")]
        s.ctx = {}  # 每种活动各自的大模型上下文：切换时存起来 / 换回来（唠嗑不会被游戏、跳舞的话带跑）
        s.rec = None  # 这一场小游戏的记录：每题谁答了什么、谁答对，每人答了几题 / 答对几题 / 得分；结束或换掉时存进 data/games.json
        threading.Thread(target=s._loop, daemon=True).start()

    def wish_on(s) -> bool:
        """礼物心愿开没开：和组件页「礼物心愿」是同一个开关（live.show.wish）。"""
        return bool((s.e.cfg["live"].get("show") or {}).get("wish"))

    # ---------- 小游戏记录
    def _rec_start(s, typ: str):
        s._rec_save()
        s.rec = {"t": time.time(), "type": typ, "name": s.GAMES[typ], "rounds": [], "players": {}}

    def _player(s, nick: str) -> dict:
        return s.rec["players"].setdefault(nick, {"answered": 0, "right": 0, "score": 0})

    def _rec_save(s):
        r = s.rec
        if not r or not r["rounds"] or r.get("saved"):
            return
        r.update(saved=True, end=time.time())
        path = s.e.data / "games.json"
        save_text(path, json.dumps((read_json(path, []) + [r])[-100:], ensure_ascii=False, indent=1))
        log(f"[活动] 已记录这场{r['name']}：{len(r['rounds'])} 轮，{len(r['players'])} 人参与")

    @staticmethod
    def rec_view(r: dict | None) -> dict | None:
        if not r:
            return None
        ps = sorted(({"nick": n, **v} for n, v in r["players"].items()), key=lambda p: (-p["score"], -p["right"], -p["answered"]))
        return {"t": r["t"], "type": r["type"], "name": r["name"], "players": ps[:100], "rounds": r["rounds"][-30:]}

    def records(s) -> list:
        return [s.rec_view(g) for g in reversed(read_json(s.e.data / "games.json", [])[-30:])]

    def cfg(s) -> dict:
        c = s.e.cfg["live"].get("activity") or {}
        return {k: {**v, **(c.get(k) or {})} for k, v in s.DEFAULT.items()}

    def view(s) -> dict:
        with s.lock:  # 答题的线程同时在改分数和玩家：不加锁可能抛异常，题念了但画面收不到通知
            return s._view()

    def _view(s) -> dict:
        g = s.g or {}
        pub = {k: g[k] for k in ("type", "round", "rounds", "phase", "lo", "hi", "guesses", "q", "opts", "deadline", "secs", "winner", "n_ans", "counts", "n_right")
               if k in g}
        if g.get("paused"):  # 暂停中：倒计时停在暂停那一刻
            pub["paused"], pub["left"] = True, max(0, round(g.get("deadline", 0) - g["paused"])) if g.get("deadline") else None
        if g.get("phase") in ("reveal", "won", "over"):  # 揭晓之后才给答案
            pub.update({k: g[k] for k in ("ans", "secret", "why", "right") if k in g})
        w = s.cfg()["wish"]
        return {"id": s.id, "name": s.NAMES[s.id], "game": pub or None, "songs": s.songs, "power": s.cfg()["dance"]["power"],
                "scores": sorted(s.scores.items(), key=lambda kv: -kv[1])[:10], "record": s.rec_view(s.rec),
                "wish": {"on": s.wish_on(), "items": [{**i, "got": min(s.got.get(i["gift"], 0), i["n"])} for i in w["items"]]}}

    def emit(s):
        EV.emit("activity", **s.view())

    def busy(s) -> bool:
        return s.e.replying > 0 or s.e.player.busy()

    def chat_mode(s) -> bool:
        return s.e.cfg["live"].get("mode") == "chat"

    # ---------- 切换活动
    def set(s, act: str, once: bool = False, quiet: bool = False, cut: bool = True) -> dict:
        """控制台切活动；once：礼物心愿兑现的跳舞，跳一首回到原来的活动；quiet：不说收尾的话（游戏结束时已经说过了）；
        cut：打断正在说的话（手动切换时；跳完、玩完自动回来时不打断）。"""
        if act not in s.NAMES:
            return {"error": "未知的活动"}
        if not s.chat_mode():
            return {"error": "活动只在唠嗑直播里用：先在唠嗑页「使用」一个人设"}
        if cut:
            s.e.stop()
        with s.lock:
            prev, s.id, s.g = s.id, act, None
            if prev == "game":  # 游戏换掉 / 结束：这场的记录存起来
                s._rec_save()
            llm = s.e.llm
            if llm and prev != act:  # 换一份上下文
                s.ctx[prev] = list(llm.history)
                llm.history = list(s.ctx.get(act, []))
            s.say_q = [x for x in s.say_q if not x.get("act")]  # 上一个活动没说完的串场话不要了
            if act == "dance":
                s.back = prev if once and prev != "dance" else "talk"
                s.songs = 1 if once else int(s.cfg()["dance"]["songs"])  # 0 = 一直跳
                m = s.e.music
                if not m.files() and not m.req:
                    log("[活动] 歌单是空的：先在「背景音乐」里上传几首歌，没有音乐只能原地摇摆", "warn")
                elif not m.playing:
                    threading.Thread(target=lambda: m.control("play"), daemon=True).start()
                s.say_q.append({"act": 1, "ask": "（现在进入舞台跳舞环节，你站到舞台中间准备跟着音乐跳舞。用一两句话热场，邀请大家发弹幕、点歌一起嗨。不要提礼物。）"})
            elif act == "game":
                s.scores = {}
                s._rec_start(s.cfg()["game"]["type"])
                s._round()
            elif prev != "talk" and not quiet:
                s.say_q.append({"act": 1, "ask": f"（{s.NAMES[prev]}环节结束了，回到唠嗑。用一句话自然地收个尾，再随口起个话头。）"})
        log(f"[活动] {s.NAMES[act]}")
        s.emit()
        return s.view()

    def context(s) -> str:
        """回答观众留言时附给大模型的「现在在干嘛」。"""
        if s.id == "dance":
            return "（现在是舞台跳舞环节，你正一边跟着音乐跳舞一边看弹幕：回答一两句就好，可以顺口邀请大家点歌。）"
        g = s.g or {}
        if s.id == "game" and g.get("type") == "guess":
            return f"（现在在玩弹幕小游戏「猜数字」，当前范围 {g.get('lo')} 到 {g.get('hi')}：回答一两句，提醒大家直接发数字参与，不要说出答案。）"
        if s.id == "game":
            return "（现在在玩弹幕答题挑战，观众发 A/B/C/D 抢答：回答一两句，不要说出正确答案。）"
        return ""

    # ---------- 跳舞：一首歌放完算一首
    def on_song(s, title: str):
        if s.id != "dance" or not s.songs:
            return
        s.songs -= 1
        if s.songs <= 0:
            s.set(s.back, cut=False)
        else:
            s.say_q.append({"act": 1, "text": f"下一首《{title.split(' - ')[-1]}》，继续跳起来！"})
            s.emit()

    # ---------- 礼物心愿
    def on_gift(s, gift: str, n: int, nick: str):
        w = s.cfg()["wish"]
        if not (s.wish_on() and s.chat_mode()):
            return
        for it in w["items"]:
            if it["gift"] and (it["gift"] in gift or gift in it["gift"]):
                s.got[it["gift"]] = s.got.get(it["gift"], 0) + n
                if s.got[it["gift"]] >= it["n"]:
                    s.got[it["gift"]] -= it["n"]
                    s.say_q.insert(0, {"text": f"心愿达成！谢谢{nick or '大家'}和各位送的{it['gift']}～说到做到：{it['reward'] or '马上安排'}！",
                                       "on": (lambda: s.set("dance", once=True)) if it.get("act") == "dance"
                                       else (lambda: s.set("game", cut=False)) if it.get("act") == "game" else None})  # 跳一支舞 / 开一局小游戏（按「弹幕小游戏」的设置）
                    log(f"[活动] 心愿达成：{it['gift']} × {it['n']} → {it['reward']}")
        s.emit()

    def reset_wish(s):
        s.got = {}
        s.emit()

    def wish_prompt(s) -> str:
        w = s.cfg()["wish"]
        if not (s.wish_on() and w["items"]):
            return ""
        items = "；".join(f"{i['gift']} 攒到 {i['n']} 个 → {i['reward']}" for i in w["items"] if i["gift"])
        return (f"\n直播间心愿（画面上有进度条）：{items}。可以偶尔自然地提一句心愿进度，但不要催促、不要求观众送礼，"
                "不说“刷礼物”“打赏”之类的话；有人说自己是未成年人就劝他别送礼物。")

    # ---------- 小游戏
    def _round(s):
        c = s.cfg()["game"]
        g = s.g or {"type": c["type"], "round": 0, "rounds": int(c["rounds"])}
        g = {"type": g["type"], "round": g["round"] + 1, "rounds": g["rounds"], "secs": int(c["secs"])}
        s.g = g
        if g["type"] == "guess":
            g.update(phase="play", secret=random.randint(1, 100), lo=1, hi=100, guesses=[], last_report=time.time(), news=0)
            s.say_q.append({"act": 1, "text": f"猜数字第 {g['round']} 轮！我心里想了一个 1 到 100 之间的数，直接发数字猜，猜中的加一分！" if g["round"] == 1
                            else f"第 {g['round']} 轮开始！还是 1 到 100，发数字来猜～"})
        else:
            g.update(phase="loading")
            threading.Thread(target=s._load_quiz, args=(g, c["topic"], c["level"]), daemon=True).start()
        s.emit()

    def _load_quiz(s, g: dict, topic: str, level: str):
        if (topic, level) != s.quiz_key:  # 换了类别或难度：之前备好的题不要了
            s.quiz_q, s.quiz_key = [], (topic, level)
        need = max(3, min(8, g["rounds"] - g["round"] + 2))
        for _ in range(80):  # 程序刚启动时大模型还在连接：等一会儿，别直接判定出不了题
            if s.e.llm or s.g is not g or not s.e.cfg["llm"].get("api_key"):
                break
            time.sleep(.5)
        if not s.quiz_q and s.e.llm:  # 现出一批要二三十秒：主播先说一句，不冷场
            s.say_q.append({"act": 1, "text": "我先想几道题，大家稍等～"})
        for _ in range(3):
            if s.quiz_q or s.g is not g or not s.e.llm:
                break
            try:
                s.quiz_q = s._make_quiz(topic, level, need)
            except Exception as e:  # noqa: BLE001
                log(f"[活动] 出题失败，重试：{type(e).__name__}: {e}", "warn")
        if s.g is not g:
            return
        if s.quiz_q:
            q = s.quiz_q.pop(0)
        elif s.e.llm or topic:  # 宁可停下，也不拿不合类别的备用题糊弄
            log(f"[活动] 出不了「{topic or '常识'}」的题，这局先结束", "warn")
            s.say_q.append({"text": "题库一时没想出来，这局先到这里，我们接着聊～"})
            return s.set("talk", quiet=True, cut=False)
        else:  # 没配大模型：用备用题
            q = next((x for x in s.QUIZ_BANK if not s._seen(x)), random.choice(s.QUIZ_BANK))
        s._remember(q)
        L = "ABCD"
        g.update(phase="ready", q=q["q"], opts=q["opts"], ans=L[q["ans"]], why=q.get("why", ""), answers={}, n_ans=0, counts=[0, 0, 0, 0])
        text = f"第 {g['round']} 题：{q['q']} " + "；".join(f"{L[i]}，{o}" for i, o in enumerate(q["opts"])) + "。发字母抢答！"

        def go():  # 题目念出来的时候开始计时（念题的时间也算进去）
            g.update(phase="play", deadline=time.time() + len(text) * .23 + g["secs"])
            s.emit()
        s.say_q.append({"act": 1, "text": text, "on": go})
        s.emit()

    # ---------- 出题：一次出一批 → 独立答一遍核对（答案、类别）→ 和考过的题查重。考过的题记在 data/quiz_asked.json，重启也不会重复
    @staticmethod
    def _qnorm(t: str) -> str:
        return re.sub(r"[\s\W_]+|通常|一般|大约|大概|主要|是什么|是哪|哪种|哪个|哪一|多少|有几|几", "", t or "")

    def _seen(s, q: dict, extra=()) -> bool:
        """考过了吗：题面或考点相似就算（「章鱼有几颗心脏」和「章鱼通常有几个心脏」是同一题）。"""
        import difflib

        nq, np_ = s._qnorm(q["q"]), s._qnorm(q.get("point", ""))
        same = lambda a, b, r: bool(a and b) and difflib.SequenceMatcher(None, a, b).ratio() >= r
        return any(same(nq, s._qnorm(x["q"]), .62) or same(np_, s._qnorm(x.get("point", "")), .7) for x in [*s.asked, *extra])

    def _remember(s, q: dict):
        s.asked = (s.asked + [{"q": q["q"], "point": q.get("point", "")}])[-500:]
        save_text(s.e.data / "quiz_asked.json", json.dumps(s.asked, ensure_ascii=False, indent=0))

    def _ask_json(s, prompt: str, temperature: float) -> dict:
        llm = s.e.llm
        r = llm.client.chat.completions.create(model=llm.model, messages=[{"role": "user", "content": prompt}], temperature=temperature,
                                               response_format={"type": "json_object"}, timeout=60)
        if getattr(r, "usage", None):
            s.e.ledger.add(r.usage, getattr(r, "model", "") or llm.model)
        return json.loads(r.choices[0].message.content)

    @staticmethod
    def _ans_index(v, opts: list) -> int | None:
        """大模型给的答案可能是序号、字母或选项原文。"""
        t = str(v).strip()
        if t.isdigit() and int(t) < 4:
            return int(t)
        if len(t) == 1 and t.upper() in "ABCD":
            return "ABCD".index(t.upper())
        return opts.index(t) if t in opts else None

    def _make_quiz(s, topic: str, level: str, need: int) -> list:
        subject = topic or "生活常识、自然、历史、地理、科技、美食里的冷知识"
        angles = "、".join(random.sample(["名字的由来", "常见的误区", "具体的数字和比例", "冷门的例外", "历史上的第一次", "不同地方的差异", "背后的科学原理", "容易混淆的两样东西"], 3))
        recent = "；".join((x.get("point") or x["q"]) for x in s.asked[-60:])
        prompt = (f"你是直播间答题节目的出题人。出 {need + 3} 道单选抢答题。\n"
                  f"【类别】全部题目必须属于：{subject}。不属于这个类别的题一道都不要出。\n"
                  f"【难度】{s.LEVELS[level]}\n"
                  f"【这批题多从这些角度想】{angles}。\n"
                  "【要求】每题四个选项，只有一个正确，答案是确定的事实、查得到、没有争议；错误选项要像真的，不能一眼排除；"
                  "题目不超过 34 字，每个选项不超过 10 字；一批里的题考点不能相近；不涉及政治、宗教、色情、暴力、医疗建议。\n"
                  + (f"【这些已经考过，考点也不要重复】{recent}\n" if recent else "")
                  + '只输出 JSON：{"items":[{"q":"题目","opts":["选项1","选项2","选项3","选项4"],"ans":正确选项序号0-3,"why":"一句话解释，不超过 25 字","point":"考点，10 字以内"}]}')
        items, good = s._ask_json(prompt, 1.0).get("items") or [], []
        for d in items:
            try:
                opts, ans = [str(o).strip()[:16] for o in d["opts"]][:4], s._ans_index(d["ans"], [str(o).strip() for o in d["opts"]])
            except (KeyError, TypeError):
                continue
            q = {"q": str(d.get("q", "")).strip()[:60], "opts": opts, "ans": ans, "why": str(d.get("why", ""))[:40], "point": str(d.get("point", ""))[:20]}
            if q["q"] and len(set(opts)) == 4 and ans is not None and not s._seen(q, good):
                good.append(q)
        if not good:
            log(f"[活动] 这批 {len(items)} 道题都不合格（格式不对或和考过的重复）", "warn")
            return []
        kept = s._verify_quiz(good, subject)
        log(f"[活动] 出题：类别「{subject[:16]}」，生成 {len(items)} 道，查重后 {len(good)} 道，核对后 {len(kept)} 道")
        for q in kept:  # 打乱选项：大模型爱把答案放在 A
            order = random.sample(range(4), 4)
            q["opts"], q["ans"] = [q["opts"][i] for i in order], order.index(q["ans"])
        return kept[:need]

    def _verify_quiz(s, qs: list, subject: str) -> list:
        """另开一轮让大模型当审核员：不看答案自己做一遍，答案对不上、或者不属于这个类别的题不要。核对失败时不拦（直播不能卡住）。"""
        body = "\n".join(f"{i}. {q['q']} 选项：" + " / ".join(f"{k}={o}" for k, o in enumerate(q["opts"])) for i, q in enumerate(qs))
        try:
            r = s._ask_json(f"你是答题节目的审核员。类别要求：{subject}。逐题独立判断：\n{body}\n"
                            '只输出 JSON：{"items":[{"i":题号,"ans":你认为正确的选项序号0-3,"on_topic":是否属于类别(true/false)}]}', 0.2)
            ok = {int(x["i"]) for x in r.get("items") or [] if x.get("on_topic") is not False and str(x.get("ans")) == str(qs[int(x["i"])]["ans"])}
        except Exception as e:  # noqa: BLE001
            log(f"[活动] 题目核对没做成，先用：{type(e).__name__}: {e}", "warn")
            return qs
        return [q for i, q in enumerate(qs) if i in ok]

    # ---------- 控制台：暂停 / 继续 / 终止
    def pause(s, on: bool) -> dict:
        g = s.g
        if s.id != "game" or not g or g.get("phase") == "over":
            return {"error": "现在没在玩小游戏"}
        with s.lock:
            if on and not g.get("paused"):
                g["paused"] = time.time()  # 排着的出题、揭晓先留着，继续后再说（暂停时只说不带 act 的话）
                s.e.stop()
                s.say_q.insert(0, {"text": "游戏先暂停一下，马上回来～"})
            elif not on and g.get("paused"):
                d = time.time() - g.pop("paused")  # 暂停了多久：倒计时、下一轮的时间都往后挪
                for k in ("deadline", "next_at", "last_report"):
                    if g.get(k):
                        g[k] += d
                s.say_q.insert(0, {"text": "好，游戏继续！" + ("接着发数字猜～" if g["type"] == "guess" else "接着抢答～")})
        log(f"[活动] 小游戏{'暂停' if on else '继续'}")
        s.emit()
        return s.view()

    def stop_game(s) -> dict:
        """终止：有人得分就直接上排行榜（念一下结果、存记录），没人得分直接回到唠嗑。"""
        g = s.g
        if s.id != "game" or not g:
            return {"error": "现在没在玩小游戏"}
        s.e.stop()
        top = sorted(s.scores.items(), key=lambda kv: -kv[1])[:3]
        if not top:
            return s.set("talk", quiet=False, cut=False)
        with s.lock:
            g.pop("paused", None)
            g.update(phase="over", next_at=time.time() + 18)
            s.say_q = [x for x in s.say_q if not x.get("act")]
            s.say_q.append({"act": 1, "text": f"{s.GAMES[g['type']]}先玩到这里！第一名是{top[0][0]}，{top[0][1]} 分" + (f"，第二名{top[1][0]}" if len(top) > 1 else "") + "，恭喜！"})
            s._rec_save()
        log("[活动] 小游戏已终止")
        s.emit()
        return s.view()

    def on_comment(s, nick: str, text: str) -> str | None:
        """弹幕留言：是这局游戏的答案就收下（返回控制台显示的状态），不是返回 None 照常排队回答。"""
        g = s.g
        if s.id == "game" and g and g.get("paused") and re.fullmatch(r"\s*(\d{1,3}|[a-dA-Dａ-ｄＡ-Ｄ])\s*[.。!！]?\s*", text):
            return "游戏：暂停中，这条不算"
        if s.id != "game" or not g or g.get("phase") != "play":
            return None
        if g["type"] == "guess":
            m = re.fullmatch(r"\s*(\d{1,3})\s*", text)
            if not m:
                return None
            n = int(m.group(1))
            with s.lock:
                if not g["lo"] <= n <= g["hi"]:
                    return f"游戏：{n} 不在 {g['lo']}~{g['hi']} 里"
                g["guesses"] = (g["guesses"] + [{"nick": nick, "n": n, "r": "=" if n == g["secret"] else "<" if n < g["secret"] else ">"}])[-6:]
                g.setdefault("who", {})[nick] = g["who"].get(nick, 0) + 1
                s._player(nick)["answered"] += 1
                if n == g["secret"]:
                    g.update(phase="won", winner=nick, lo=n, hi=n, next_at=time.time() + 4)
                    s.scores[nick] = s.scores.get(nick, 0) + 1
                    p = s._player(nick)
                    p["right"], p["score"] = p["right"] + 1, p["score"] + 1
                    s.rec["rounds"].append({"n": g["round"], "secret": n, "winner": nick, "guesses": sum(g["who"].values()),
                                            "answers": {k: f"猜了 {v} 次" for k, v in list(g["who"].items())[:60]}, "right": [nick]})
                    s.say_q.insert(0, {"act": 1, "text": f"恭喜{nick}猜中了！就是 {n}！加一分～"})
                elif n < g["secret"]:
                    g["lo"], g["news"] = n + 1, g["news"] + 1
                else:
                    g["hi"], g["news"] = n - 1, g["news"] + 1
            s.emit()
            return f"游戏：猜 {n}" + ("，猜中了！" if n == g["secret"] else "，小了" if n < g["secret"] else "，大了")
        m = re.fullmatch(r"\s*([a-dA-Dａ-ｄＡ-Ｄ])\s*[.。!！]?\s*", text)
        if not m:
            return None
        k = "ABCD".index(unicodedata.normalize("NFKC", m.group(1)).upper())
        with s.lock:
            if nick in g["answers"]:
                return "游戏：已经答过了"
            g["answers"][nick] = (k, time.time())
            g["n_ans"], g["counts"][k] = len(g["answers"]), g["counts"][k] + 1
            s._player(nick)["answered"] += 1
        s.emit()
        return f"游戏：答 {'ABCD'[k]}"

    def _loop(s):
        while True:
            time.sleep(.5)
            try:
                s._tick()
            except Exception as e:  # noqa: BLE001
                log(f"[活动] 出错：{type(e).__name__}: {e}", "error")

    def _tick(s):
        g, now = s.g, time.time()
        if s.id != "game" or not g or g.get("paused"):  # 暂停中：不揭晓、不报范围、不进下一轮
            return
        if g["type"] == "guess" and g["phase"] == "play" and g["news"] and now - g["last_report"] > 10 and not s.say_q and not s.busy():
            last = g["guesses"][-1]
            g.update(news=0, last_report=now)
            s.say_q.append({"act": 1, "text": f"{last['nick']}猜了 {last['n']}，{'小了' if last['r'] == '<' else '大了'}！现在范围是 {g['lo']} 到 {g['hi']}。"})
        if g["type"] == "quiz" and g["phase"] == "play" and now > g["deadline"]:
            L, k = "ABCD", "ABCD".index(g["ans"])
            with s.lock:
                right = sorted((t, n) for n, (a, t) in g["answers"].items() if a == k)
                for i, (_, n) in enumerate(right):
                    s.scores[n] = s.scores.get(n, 0) + (2 if i == 0 else 1)
                    p = s._player(n)
                    p["right"], p["score"] = p["right"] + 1, p["score"] + (2 if i == 0 else 1)
                names = [n for _, n in right]
                s.rec["rounds"].append({"n": g["round"], "q": g["q"], "opts": g["opts"], "ans": g["ans"], "why": g.get("why", ""),
                                        "answers": {n: L[a] for n, (a, _) in list(g["answers"].items())[:60]}, "right": names[:60]})
                g.update(phase="reveal", right=names[:30], n_right=len(names), next_at=now + 6)
            s.say_q.append({"act": 1, "text": f"时间到！答案是 {g['ans']}，{g['opts'][k]}。{g['why']}"
                            + (f"答对的有{'、'.join(names[:8])}{f'等 {len(names)} 人' if len(names) > 8 else f'，一共 {len(names)} 人' if len(names) > 1 else ''}，{names[0]}最快，多加一分！"
                               if names else "这题没人答对，下一题加油～")})
            s.emit()
        if g["phase"] in ("won", "reveal") and now > g.get("next_at", 0) and not s.say_q and not s.busy():
            if g["round"] >= g["rounds"]:
                top = sorted(s.scores.items(), key=lambda kv: -kv[1])[:3]
                g.update(phase="over", next_at=now + 25)
                s._rec_save()
                s.say_q.append({"act": 1, "text": f"{s.GAMES[g['type']]}结束！" + (f"第一名是{top[0][0]}，{top[0][1]} 分" + (f"，第二名{top[1][0]}" if len(top) > 1 else "")
                                + "，恭喜恭喜！" if top else "这次没人得分，下次再来～") + "我们回来接着聊～"})
                s.emit()
            else:
                s._round()
        elif g["phase"] == "over" and now > g["next_at"] and not s.say_q and not s.busy():
            s.set("talk", quiet=True, cut=False)

    def speak_next(s) -> bool:
        """弹幕那边空闲时调用：说一句排着的串场话（小游戏暂停中：游戏的话先不说）。"""
        i = next((i for i, x in enumerate(s.say_q) if not (s.g and s.g.get("paused") and x.get("act"))), None)
        if i is None:
            return False
        x = s.say_q.pop(i)
        if x.get("on"):
            x["on"]()
        if x.get("ask"):
            s.e.host(x["ask"])
        elif x.get("text"):
            s.e.say(x["text"])
        return True


class Engine:
    def __init__(s, cfg: dict, root: Path):
        s.cfg, s.root = cfg, root
        s.models, s.data = root / "models", root / "data"
        s.player, s.render_q = Player(), queue.Queue()
        s.player.on_start = None  # 每句开始播放时回调（导播在 Showcase 建好后接上）
        s.player.av_offset = cfg.get("av", {}).get("offset_ms", 0) / 1000
        s.player.out.device, s.player.out.monitor = cfg["live"]["device"], cfg["live"]["monitor"]
        s.llm = s.tts = s.mic = None
        s.player.param = True  # 画面由界面的 Live2D 自己画，这里只放声音、发口型时间轴
        s.ready, s.mode, s.kind = False, "live2d", "live2d"
        s.live_url = ""  # 直播画面地址（main.serve 绑定端口后填入）
        s.shop = {}  # 使用中的店铺档案（main 在启动和切换店铺时设置）
        s.persona = {}  # 唠嗑直播的人设（main 在启动和「使用」人设时设置）
        s.history_file = s.data / "chat_history.json"
        s.history = read_json(s.history_file, [])
        s.replying, s._rlock = 0, threading.Lock()  # 正在生成回答的轮数（大模型还没说完也算忙，弹幕排队用）
        s.danmaku = Danmaku(s)
        s.music = Music(s)
        s.corpus = Corpus(s.data / "corpus.db", s.history)
        s.ledger = Ledger(s, s.data / "ledger.json")
        s.show = Showcase(s)
        s.act = Activity(s)  # 唠嗑直播的活动：跳舞 / 小游戏 / 礼物心愿
        s.player.on_start = s._on_seg

    @property
    def showcase(s) -> str:
        return s.show.id


    # ---------- 启动
    def start(s):
        threading.Thread(target=s._load, daemon=True).start()
        threading.Thread(target=s._monitor, daemon=True).start()
        threading.Thread(target=s._render_loop, daemon=True).start()

    def _load(s):
        try:
            s.tts = make_tts(s.cfg["tts"], s.models)
            s._connect_llm()
            s.ready = True
            EV.emit("status", state="ready", note="就绪")
            log("[启动] 语音和大模型就绪")
        except Exception as e:  # noqa: BLE001
            log(f"[启动] 失败：{type(e).__name__}: {e}", "error")
            EV.emit("status", state="error", note=str(e))


    def _monitor(s):
        while True:
            EV.emit("sys", gpu="", vram_used=0, vram_total=0, torch_alloc=0, torch_reserved=0, mode=s.mode, avatar=s.kind, ready=s.ready,
                    speaking=s.player.speaking, mic=False, llm=bool(s.llm), modes={})
            time.sleep(2)

    def _connect_llm(s, fresh=False):
        try:
            chat = s.cfg["live"].get("mode") == "chat"  # 唠嗑直播：不带店铺资料
            s.llm = LLM(s.cfg["llm"], build_chat_system(s.cfg, s.persona) + s.act.wish_prompt() if chat else build_system(s.cfg, s.shop))
            s.llm.mode = "chat" if chat else "shop"  # 用的是哪种直播的提示词（回答前核对，防止切换时序出错串台）
            # 接上历史对话的上下文（最近 6 轮）；fresh = 换了店，从零开始
            s.llm.history = [] if fresh else [{"role": m["role"], "content": m["text"]} for m in s.history[-12:]]
            s.llm.on_usage = s.ledger.add
            if chat and getattr(s, "act", None) and s.act.id != "talk":  # 正在跳舞 / 玩游戏：用这个活动自己的上下文
                s.llm.history = list(s.act.ctx.get(s.act.id, []))
            s.ledger.refresh_balance(force=True)
            log(f"[启动] LLM 就绪（{s.cfg['llm']['model']}）")
        except RuntimeError as e:
            s.llm = None
            log(f"[启动] {e}，先用复读模式", "warn")

    # ---------- 对话历史：保存在 data/chat_history.json，重启后还在
    def _record(s, role, text, turn, source="text", name=None, test=False):
        """发事件给界面，同时记进历史；助手一轮里的多句话合并成一条。name：观众昵称（弹幕 / 模拟留言）。
        test：控制台测试留言，界面照常显示，但不写进对话记录。"""
        e = {"role": role, "text": text, "turn": turn, "source": source, "t": time.time(), **({"name": name} if name else {})}
        if test:
            EV.emit("chat", **{k: v for k, v in e.items() if k != "t"}, test=True)
            return
        if role == "assistant" and s.history and s.history[-1]["turn"] == turn and s.history[-1]["role"] == "assistant":
            s.history[-1]["text"] += text
        else:
            s.history.append(dict(e))
        save_text(s.history_file, json.dumps(s.history, ensure_ascii=False, indent=1))
        EV.emit("chat", **{k: v for k, v in e.items() if k != "t"})

    def clear_history(s):
        s.stop()
        s.history = []
        s.history_file.unlink(missing_ok=True)
        if s.llm:
            s.llm.history = []
        EV.emit("cleared")
        log("[对话] 已清空对话记录和上下文")

    def export_history(s) -> Path:
        out = s.data / "exports" / f"对话记录_{time.strftime('%Y%m%d-%H%M%S')}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# 与{s.cfg.get('name', '悠悠')}的对话记录\n", f"导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"]
        for m in s.history:
            who = "我" if m["role"] == "user" else s.cfg.get("name", "悠悠")
            lines.append(f"**{who}**（{time.strftime('%m-%d %H:%M', time.localtime(m['t']))}）：{m['text']}\n")
        out.write_text("\n".join(lines), encoding="utf-8")
        log(f"[对话] 已导出到 {out}")
        return out

    # ---------- 设置：界面改完即时生效（持久化由 main.py 写 data/settings.json）
    def apply_settings(s, patch: dict):
        for k, v in patch.items():  # 浅合并到各分组
            if isinstance(v, dict):
                s.cfg.setdefault(k, {}).update(v)
            else:
                s.cfg[k] = v
        if "tts" in patch and s.tts:
            release_tts(s.cfg["tts"].get("engine", "edge"))
            s.tts = make_tts(s.cfg["tts"], s.models)
        if "av" in patch:
            s.player.av_offset = s.cfg["av"]["offset_ms"] / 1000
        if "live" in patch:
            s.player.out.device, s.player.out.monitor = s.cfg["live"]["device"], s.cfg["live"]["monitor"]
            if {"device", "monitor"} & set(patch["live"]):
                s.music.restart()
            if "mode" in patch["live"] and s.llm:  # 店铺直播 ↔ 唠嗑：换一套系统提示词，从零开始聊（不带另一种模式的上下文）
                s._connect_llm(fresh=True)
                log(f"[直播] 已切换为{'唠嗑直播' if s.cfg['live']['mode'] == 'chat' else '店铺直播'}")
        if "avatar" in patch:
            s.emit_avatar()
        if "vad" in patch and s.mic:
            s.mic.vad.min_silence_samples = SR * int(s.cfg["vad"]["silence_ms"]) / 1000
        if {"activity", "show"} & set(patch.get("live", {})) and s.llm and s.cfg["live"].get("mode") == "chat":  # 礼物心愿开关 / 内容改了：提示词里的心愿跟着换
            s._connect_llm()
            s.act.emit()
        if "llm" in patch or "persona" in patch:
            s._connect_llm()
        if "name" in patch:
            EV.emit("shop")  # 直播画面上的掌柜名字
        if "live" in patch:
            EV.emit("live", auto=s.cfg["live"]["auto"], follow=s.cfg["live"]["follow"])  # 直播画面重新取场景、背景、显示元素；控制台同步接管状态
        if patch.get("render", {}).get("mode"):
            s.set_mode(patch["render"]["mode"])
        log("[设置] 已保存并生效")

    def set_shop(s, shop: dict):
        """切换 / 保存了使用中的店铺：掌柜换一套知识，直播画面换店名、logo、公告。
        换了店（或改了店名）时不带旧对话当上下文：否则大模型会照抄之前回答里的旧店名、旧价格。"""
        renamed = shop.get("id") != s.shop.get("id") or shop.get("name") != s.shop.get("name")
        s.shop = shop
        s._connect_llm(fresh=renamed)
        EV.emit("shop")

    def set_showcase(s, sid: str):
        """控制台手动切换展示场景（见 Showcase）。"""
        return s.show.manual(sid)

    def set_persona(s, persona: dict):
        """换了唠嗑人设：唠嗑中就换一套系统提示词、从零开始聊（不带上一个人设的口吻）；直播画面换主播名。"""
        s.persona = persona
        if s.cfg["live"].get("mode") == "chat":
            s._connect_llm(fresh=True)
        EV.emit("shop")

    def host(s, ask: str):
        """活动串场：给大模型一段说明，让主播自己说几句（不当成观众留言，聊天记录里也不显示提问）。"""
        turn = {"id": int(time.time() * 1000), "t0": time.perf_counter(), "source": "host", "name": "", "q": ask}
        threading.Thread(target=s._reply, args=(ask, s.player.gen, turn), daemon=True).start()

    def cold_talk(s, topic: str):
        """唠嗑冷场：没人说话时主播主动起个话题（这条提示只发给大模型，聊天记录里不显示成观众留言）。"""
        if not s.llm:  # 没配大模型时不开聊，否则会把这段提示当台词念出来
            return
        turn = {"id": int(time.time() * 1000), "t0": time.perf_counter(), "source": "cold", "name": "", "q": f"（冷场主动开聊）{topic}"}
        ask = f"（直播间这会儿没人说话。你主动起个轻松的话题，聊聊「{topic}」：先说一两句自己的想法，再顺口问观众一句。别提“冷场”。）"
        threading.Thread(target=s._reply, args=(ask, s.player.gen, turn), daemon=True).start()

    def preview_voice(s, tts_cfg: dict):
        """试听：用设置面板里还没保存的引擎 / 音色 / 语速 / 音调念一句。"""
        if s.tts and not s.player.busy():
            tts = make_tts({**s.cfg["tts"], **tts_cfg}, s.models)
            line = f"你好，我是{s.cfg.get('name', '悠悠')}，欢迎来到直播间，这是我现在的声音。"
            tts.strict = True
            s.player.out.play(tts(line))

    # ---------- 更换形象：照片或视频，两个引擎都更新

    # ---------- 界面操作
    def chat(s, text: str, source="text", t0=None, name=None, pre=False, batch=None, brief=False, hint="", test=False):
        """回答一条留言。pre = 预生成：上一条还在说，不打断它，这条合成好排在后面；
        batch = 积压时几条一起回 [(昵称, 内容), …]；brief = 后面还有人排队，这次说短点。"""
        text = text.strip()
        if not text:
            return
        turn = {"id": int(time.time() * 1000), "t0": t0 or time.perf_counter(), "test": test}
        if source == "danmaku" and not s.cfg["live"]["auto"]:  # 人工接管中：观众留言只记下来，不自动回答
            s._record("user", text, turn["id"], source, name, test)
            return
        if not pre:
            s.stop()
            s.show.pending, s.show.after = None, []
        for nick, t in batch or [(name, text)]:
            s._record("user", t, turn["id"], source, nick, test)
        s.show.link(text, pre)
        turn.update(q=text, source=source, name=name or "", pre=pre)
        ask = f"（直播间观众「{name}」发来弹幕）{text}" if name else text  # 带上昵称，掌柜可以点名回应
        if batch:
            ask = ("（弹幕有点多，这几条一起回：" + "；".join(f"「{n}」说：{t}" for n, t in batch)
                   + "。挨个点名简短回应，同一类的问题合起来说，总共不超过 3 句。）")
        elif brief:
            ask += "\n（后面还有观众在排队，这次一两句说完。）"
        if hint:  # 这条留言的特别说明（如假礼物：开玩笑，不道谢）
            ask += "\n" + hint
        plan = s.show.plan(text, bool(batch))  # 导播：程序先判断问整类 / 共同叫法 / 孩子年龄，附上清单（规则在「店铺 → 导播」里调）
        turn.update(plan["turn"])
        ask += "".join("\n" + h for h in plan["hints"])
        if s.cfg["live"].get("mode") == "chat" and s.act.context():  # 唠嗑里正在跳舞 / 玩游戏：回答短一点、别说出答案
            ask += "\n" + s.act.context()
        threading.Thread(target=s._reply, args=(ask, s.player.gen, turn), daemon=True).start()

    def _on_seg(s, seg):
        """一句开始播放：预生成的话这时才记进聊天记录；再交给导播执行这句的画面指令。"""
        if getattr(seg, "rec", None):
            s._record("assistant", seg.rec, seg.turn["id"], test=seg.turn.get("test", False))
        s.show.on_segment(seg)

    def viewer(s, name: str, text: str):
        """控制台的模拟留言：和真实弹幕走同一条队列（去重、过滤、排队），但不计入对话记录、语料和大模型上下文。"""
        s.danmaku.push([{"type": "comment", "nick": name, "text": text, "test": True}])

    def reload(s, what: str) -> dict:
        """控制台「重新加载」：某个功能卡住时单独重来，不影响别的。画面类（page / avatar）通知直播画面和预览自己重载。"""
        if what in ("page", "avatar"):
            EV.emit("reload", what=what)
        elif what == "llm":
            s._connect_llm()
        elif what == "tts":
            s.stop()
            release_tts(s.cfg["tts"].get("engine", "edge"))
            s.tts = make_tts(s.cfg["tts"], s.models)
        elif what == "music":
            s.music._start() if s.music.playing else None
        else:
            return {"error": "未知的功能"}
        log(f"[重新加载] {dict(page='直播画面', avatar='人物形象', llm='大模型', tts='语音合成', music='背景音乐')[what]}")
        return {"ok": True}

    def say(s, text: str):
        """人工接管：让掌柜原样说出这段话（照样有口型、字幕、聊天记录），不经过大模型。"""
        text = text.strip()
        if text:
            s.stop()
            turn = {"id": int(time.time() * 1000), "t0": time.perf_counter(), "source": "say"}
            threading.Thread(target=s._reply, args=(None, s.player.gen, turn, text), daemon=True).start()

    def stop(s):
        s.player.interrupt()

    def set_mic(s, on: bool):
        if s.mic:
            s.mic.set_on(on)
            log(f"[麦克风] {'已打开，说完自动发送' if on else '已关闭'}")

    def set_mode(s, mode: str): ...

    def set_kind(s, kind: str, card: str | None = None):
        """首页选形象类型。真人：加载口型引擎；Live2D / VRM / Rive：卸载口型引擎腾出显存，播放器改为只发口型时间轴。
        card 是首页上点的卡片（同一类型分人物 / 动物两张），记下来给首页标“上次使用”。"""
        if kind in KINDS:
            s.stop()
            s.kind, s.cfg["avatar_kind"] = kind, card or kind
            s.render_q.put(("kind", kind))
            s.emit_avatar()

    def avatar_state(s) -> dict:
        """当前显示的形象（直播画面页跟着它走）：类型、角色文件、兽耳（只有首页点的是“3D 兽耳角色”才戴）。"""
        a = s.cfg["avatar"]
        return {"type": s.kind, "path": a.get(s.kind) if s.kind != "real" else None,
                "ears": a.get("vrm_ears", "cat") if s.cfg.get("avatar_kind") == "vrm_animal" else "", "name": s.cfg.get("name", "悠悠")}

    def emit_avatar(s):
        EV.emit("avatar", **s.avatar_state())

    # ---------- 流程

    def _reply(s, *a, **k):
        with s._rlock:
            s.replying += 1
        try:
            s._reply_turn(*a, **k)
        finally:
            with s._rlock:
                s.replying -= 1

    def _reply_turn(s, text, gen, turn, script=None):
        """回答一轮：script 不为空时是人工代说，按句切开直接念，并补进大模型的上下文里（它知道自己“说过”什么）。"""
        if not s.ready:
            log("画面还在加载，先只出文字和声音", "warn")
        if s.llm and getattr(s.llm, "mode", None) != s.cfg["live"].get("mode", "shop"):  # 提示词和直播类型对不上（启动加载时切过模式）：重连
            log("[对话] 直播类型和提示词不一致，已重新连接大模型", "warn")
            s._connect_llm(fresh=True)
        if script:
            sentences = iter(re.findall(r"[^。！？!?；;\n]+[。！？!?；;\n]?", script) or [script])
            if s.llm:
                s.llm.history.append({"role": "assistant", "content": script})
        elif s.llm:
            sentences = s.llm.sentences(text, cancelled=lambda: s.player.gen != gen, keep=not turn.get("test"))
        else:  # 没配 Key 时复读用户的话，方便先测语音和画面链路
            log("LLM 未配置（config.yaml 填 llm.api_key），复读模式", "warn")
            sentences = iter(re.findall(r"[^。！？!?；;\n]+[。！？!?；;\n]?", text) or [text])
        prev, spoken, first, done, carry = None, [], None, False, []
        try:
            EV.emit("stage", name="llm", state="running")
            for i, raw in enumerate(sentences):
                if s.player.gen != gen:
                    break
                spoken.append(raw)  # 语料库存原样（带指令），以后能拿来训练导播
                cues, sent = Showcase.split(raw)
                carry += cues
                if not re.search(r"\w", clean_for_speech(sent)):  # 只有指令或标点、没有话：挂到下一句（纯标点送语音会报错，整轮就断了）
                    continue
                if i == 0:
                    first = time.perf_counter() - turn["t0"]
                    EV.emit("stage", name="llm", state="done", ms=first * 1000, note="首句")
                if not turn.get("pre"):
                    s._record("assistant", sent, turn["id"], test=turn.get("test", False))
                t = time.perf_counter()
                EV.emit("stage", name="tts", state="running", note=sent[:12])
                words = []
                pcm = s.tts(sent, words)
                EV.emit("stage", name="tts", state="done", ms=(time.perf_counter() - t) * 1000, note=f"{len(pcm) / OUT_SR:.1f}s 语音")
                if s.player.gen != gen:
                    break
                if turn.get("pre") and prev is None:
                    pcm = np.concatenate([np.zeros(int(OUT_SR * .6), np.float32), pcm])  # 两条回答之间停 0.6 秒
                seg = Segment(sent, pcm, gen, turn, prev)
                seg.rec = sent if turn.get("pre") else None
                seg.words, seg.cues, carry = words, carry, []
                prev = seg
                if s.ready:
                    s.player.pending.append(seg)
                    s.render_q.put(seg)
                else:  # 画面还没就绪：只放声音，导播指令照样在这句开口时执行
                    s._on_seg(seg)
                    s.player.out.play(pcm)
                    s.player.out.wait()
            else:
                done = True
                turn["tts_done"] = True  # 整轮都合成好了：画面知道还要说多久
                s.show.pace(turn)
            if carry and s.player.gen == gen:  # 回答最后的指令（如〔看：主场景〕）：说完再执行
                s.show.after = carry
        except Exception as e:  # noqa: BLE001
            log(f"[对话] 出错：{type(e).__name__}: {e}", "error")
        try:  # 写进语料库：大模型的回答带上它看到的全部上下文；人工代说的固定话术也记一下（导出时默认不要）
            if spoken and (script or s.llm) and not turn.get("test"):  # 控制台测试留言不进语料
                s.corpus.add("script" if script else "llm", turn.get("source", "text"), turn.get("name", ""), turn.get("q", ""),
                             "".join(spoken), done and s.player.gen == gen, None if script else getattr(s.llm, "last_msgs", None),
                             "" if script else s.llm.model, "唠嗑" if s.cfg["live"].get("mode") == "chat" else s.shop.get("name", ""), first)
        except Exception as e:  # noqa: BLE001
            log(f"[语料] 记录失败：{e}", "error")

    def _render_loop(s):
        while True:
            item = s.render_q.get()
            if isinstance(item, tuple) or item.gen != s.player.gen:
                continue
            try:  # 算口型时间轴，画面由界面的 Live2D 自己画
                item.track = mouth_track(item.pcm, item.words, item.text)
                item.done = True
            except Exception as e:  # noqa: BLE001
                log(f"[画面] 出错：{type(e).__name__}: {e}", "error")
                if item in s.player.pending:  # 这句放弃：留在队列里会一直“在说话”，后面的弹幕永远排不上
                    s.player.pending.remove(item)

