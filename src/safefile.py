"""数据文件的安全读写：写入先落临时文件再整体替换，几个线程同时写、写到一半断电都不会把文件写坏；
读取时文件坏了也尽量救回来（去掉结尾多余的字符 → 上一次的备份），不让一个坏文件卡死整个程序。"""
import json
import os
import shutil
import threading
import time
from pathlib import Path

_lock = threading.RLock()
_bak_t = {}  # 文件 → 上次备份的时间


def save_text(path: Path, text: str):
    path = Path(path)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if path.exists() and time.time() - _bak_t.get(path, 0) > 60:  # 一分钟最多留一次备份（聊天记录写得很勤）
            _bak_t[path] = time.time()
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        for i in range(20):  # Windows 上别的程序（杀毒、读取中）占着文件时替换会失败，等一下再试
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == 19:
                    raise
                time.sleep(.05)


def _parse(path: Path):
    """整个解析；不行就取开头那段完整的 JSON（两个写入撞车时会在结尾留下多余字符）。解析不了返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return json.JSONDecoder().raw_decode(text.lstrip())[0]
    except ValueError:
        return None


def read_json(path: Path, default):
    """读 JSON。文件不存在返回 default；坏了就救：能救回来的话写回去，原文件留一份 .corrupt-时间 供排查。"""
    path = Path(path)
    with _lock:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            pass
        keep = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, keep)
        data = _parse(path)
        src = "结尾多余内容已去掉"
        if data is None:
            data, src = _parse(path.with_name(path.name + ".bak")), "用上一次的备份恢复"
        if data is None:
            print(f"[存档] {path.name} 损坏且无法恢复，已留存为 {keep.name}，这次按默认值启动", flush=True)
            return default
        print(f"[存档] {path.name} 损坏，{src}（原文件留存为 {keep.name}）", flush=True)
        save_text(path, json.dumps(data, ensure_ascii=False, indent=1))
        return data
