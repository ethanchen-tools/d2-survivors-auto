"""
D2 Survivors Auto - 簡易圖片搜尋 + 自動點擊
安裝:  pip install PySide6 opencv-python numpy pywin32 mss
圖片放在本檔同目錄的 images/ 資料夾 (例如 images/skill.png)
熱鍵(選用, pip install keyboard): F6 開始 / F7 暫停繼續 / F8 停止
按鍵流程 (Tab / Enter) 在介面上編輯, 自動存到本檔同目錄的 d2auto_config.json, 下次開啟自動讀取

流程: 1. 尋找傳點 (step1.png 找不到就改用 step1-1.png) -> 2. 尋找關卡章節 -> 執行自選關卡選擇 -> 3. 搜尋離開紅門 -> 4. 結束這局 (step4.png 找不到改用 step4-1.png, 搜尋到後背景按空白鍵 1 次) (循環)
  另外隨時偵測:
    step5.png = 鐵匠 技能 包包防卡 -> 按「空白鍵 / ESC」離開 (預設 空白鍵 1 次) -> 回到「3. 搜尋離開紅門」開始
    step6.png = 死亡畫面 -> 按「空白鍵 / ESC」離開 (預設 ESC 1 次) -> 回到「1. 尋找傳點」開始 (死亡次數 +1)
  介面上方可按住 W / A / S / D 按鈕, 或直接按鍵盤 WASD, 背景送鍵讓遊戲移動 (不搶焦點)
  防卡機制: 超過 X 分鐘沒有成功通關 (關卡數 +1) 就背景按「空白鍵 / ESC」(預設 空白鍵 1 次), 在「統計 / 設定」分頁設定
  自動副手戰吼: 打開包包旁的按鈕, 每隔幾秒快速按某個鍵幾次 (手操衝層用)
執行紀錄 / 錯誤原因會寫在 d2auto_log.txt
"""
import sys, os, time, threading, ctypes, json, traceback, faulthandler, queue
import urllib.request, urllib.error
from pathlib import Path

APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "d2auto_config.json"
DISCORD_PATH = APP_DIR / "discord_config.json"   # Discord 通知設定 (填 webhook / 你的 ID)
CLEAR_PATH = APP_DIR / "clear_count.json"        # 總通關數 (重開程式不歸零)
LOG_PATH = APP_DIR / "d2auto_log.txt"          # 執行紀錄 / 崩潰原因 (長時間跑出問題時看這個)


def log_event(text):
    """重要事件與錯誤都寫進檔案, 程式若突然消失也能事後追原因"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {text}\n")
    except Exception:
        pass


def _trim_log():
    """紀錄檔超過 2MB 就換成舊檔, 避免越寫越大"""
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2 * 1024 * 1024:
            LOG_PATH.replace(LOG_PATH.with_name("d2auto_log.old.txt"))
    except Exception:
        pass


def _excepthook(tp, val, tb):
    log_event("未攔截的錯誤\n" + "".join(traceback.format_exception(tp, val, tb)))


def _thread_excepthook(args):
    log_event("執行緒錯誤\n" + "".join(
        traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))


sys.excepthook = _excepthook
if hasattr(threading, "excepthook"):
    threading.excepthook = _thread_excepthook

# 連原生層面的崩潰 (access violation 之類) 也會留下紀錄, 方便查「程式突然關掉」的原因
try:
    _trim_log()
    _FH = open(LOG_PATH, "a", encoding="utf-8")
    faulthandler.enable(_FH)
except Exception:
    _FH = None


# 自動移除/隱藏後方的 CMD 黑色終端機視窗 (若以 python.exe 啟動)
def hide_console_window():
    try:
        whnd = ctypes.windll.kernel32.GetConsoleWindow()
        if whnd != 0:
            ctypes.windll.user32.ShowWindow(whnd, 0)  # 0 = SW_HIDE (隱藏黑窗)
    except Exception:
        pass

# 啟動時立刻執行隱藏黑視窗
hide_console_window()

import cv2
import numpy as np
import mss
import win32api, win32con, win32gui, win32process
from PySide6.QtCore import Qt, QThread, QTimer, Signal, QObject, QEvent
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QFrame, QComboBox,
    QSpinBox, QDoubleSpinBox, QCheckBox, QListWidget, QTabWidget, QSizePolicy, QFormLayout, QGridLayout, QScrollArea,
    QDialog, QDialogButtonBox,
)

# ============================ 設定區 ============================
WINDOW_TITLE = "D2 Survivors"      # 視窗標題關鍵字
# 圖片位置 (支援打包成 EXE):
#   1) EXE 旁邊的 images/ 資料夾 (可自行替換圖片, 不用重新打包) -> 優先
#   2) 打包進 EXE 內的 images/ (--add-data)
if getattr(sys, "frozen", False):
    _EXE_DIR = Path(sys.executable).parent
    IMAGE_DIRS = [_EXE_DIR / "images", Path(getattr(sys, "_MEIPASS", _EXE_DIR)) / "images"]
else:
    IMAGE_DIRS = [Path(__file__).resolve().parent / "images"]
IMAGE_DIR = IMAGE_DIRS[0]


def find_image(name):
    for d in IMAGE_DIRS:
        p = d / name
        if p.exists():
            return p
    return IMAGE_DIRS[0] / name

# 流程: 依序「搜尋圖片 -> 找到就點擊」, 然後下一步, 全部做完會重頭循環
# 依指示：不要用消失來判斷有沒有成功，只要有讀取到並且有點擊就算成功！
WORKFLOW = [
    {
        # 步驟 1: 圖片 1 (點完即開始搜尋圖片 2)
        "name": "1. 尋找傳點",       # 顯示在 GUI 的名稱
        "image": "step1.png",        # images/ 內的比對圖片
        "alt_images": ["step1-1.png"],   # 備用圖: step1.png 讀不到時改試這些 (images/ 內沒有該檔就略過)
        "next": "點擊完成，接著搜尋「2. 尋找關卡章節」",  # 顯示在 GUI 的下一步說明
        "threshold": 0.70,           # 相似度門檻 (0~1)
        "scale_range": (0.6, 1.4),   # 自動尋找縮放比例 (遊戲畫面和圖片大小不同也能認)
        "timeout": 3,                # 搜尋圖片 3 秒，沒有搜尋到就跳下一個步驟
        "offset": (0, 0),            # 點擊位置相對圖片中心的偏移 (x, y)
        "verify": None,              # 不用消失判斷，讀取到且點擊就算成功
        "after_wait": 1.0,           # 點擊後等待秒數
    },
    {
        # 步驟 2: 全新圖片 2 (分頁 III 按鈕，成功點到才按鍵，失敗則略過按鍵)
        "group": "B",                # 與按鍵操作綁定成同一組
        "name": "2. 尋找關卡章節",
        "image": "step2.png",        # (備用) images/step2.png
        "variant_image": "step2-{v}.png",   # 實際使用: 介面選 A1~A5 -> images/step2-A1.png ... step2-A5.png
        "key_trigger": True,         # 點擊成功後, 等待秒數由介面「圖2點擊後等待」決定
        "next": "點擊成功後等待設定秒數，執行「執行自選關卡選擇」",
        "threshold": 0.70,           # 門檻 (原生提取 1.00，0.70 容錯充足)
        "scale_range": (0.6, 1.4),   # 支援各縮放比
        "timeout": 3,                # 搜尋圖片 3 秒，沒有搜尋到就跳下一步 (且略過按鍵)
        "offset": (0, 0),
        "verify": None,              # 不用消失判斷，讀取到且點擊就算成功！
        "after_wait": 2.0,           # 依指示：點到圖片後延遲 2 秒才進行鍵盤操作
    },
    {
        # 按鍵流程 (與圖片 2 綁定: 圖片 2 成功點到才執行, 失敗則略過)
        # 內容由介面上的「按鍵流程」設定, 存在 d2auto_config.json
        "group": "B",
        "type": "keys",
        "mode": "custom",
        "name": "執行自選關卡選擇",
        "next": "依介面設定的按鍵流程",
        "starts_battle": True,       # 這步做完 = 進入戰鬥 (卡死偵測: 戰鬥中不因為久沒點擊而報卡死)
        "after_wait": 1.0,           # 做完後等待秒數
    },
    {
        # 步驟 3: 原本的步驟 2 往後挪成步驟 3 (局部切片匹配，被遮住也能認)
        "name": "3. 搜尋離開紅門",
        "ends_battle": True,         # 點到離開紅門 = 戰鬥結束 (卡死偵測用)
        "image": "step3.png",
        "next": "點擊「3. 搜尋離開紅門」",
        "threshold": 0.65,           # 圖案會閃動, 門檻設低一點
        "timeout": 3,                # 搜尋圖片 3 秒，沒有搜尋到就跳下一步
        # --- 被遮住也要找得到 ---
        "match_mode": "partial",     # "full": 整張比對 / "partial": 切成小區塊, 部分被遮也能認
        "grid": (3, 3),              # 切成 3x3 共 9 塊
        "min_hits": 2,               # 至少幾塊對上且位置一致才算找到
        "patch_threshold": 0.65,     # 每個小區塊的相似度門檻
        "tolerance": 15,             # 各區塊推算出的中心點允許誤差(像素)
        "scale_range": (0.6, 1.4),   # 自動尋找縮放比例
        "offset": (0, 0),
        "verify": None,              # 不用消失判斷，讀取到且點擊就算成功
        "after_wait": 1.0,
    },
    {
        # 步驟 4: 原本的步驟 3 往後挪成步驟 4 (結束這一局)
        "name": "4. 結束這局",
        "count_round": True,         # 這一步確認點擊成功 = 一個關卡結束, 關卡次數 +1
        "image": "step4.png",
        "alt_images": ["step4-1.png"],   # 備用圖: step4.png 讀不到時改試 step4-1.png (images/ 內沒有該檔就略過)
        "action": "key",             # 搜尋到後不點擊, 改成背景送出按鍵
        "key": "space",              # 送空白鍵
        "key_count": 1,              # 按 1 次
        "next": "搜尋到「4. 結束這局」(step4 / step4-1)，背景按空白鍵 1 次，完成後循環回「1. 尋找傳點」",
        "threshold": 0.75,           # 固定文字按鈕
        "scale_range": (0.6, 1.4),
        "timeout": 3,                # 搜尋圖片 3 秒，沒有搜尋到就跳下一步 (循環回步驟1)
        "offset": (0, 0),
        "verify": None,              # 不用消失判斷，讀取到且點擊就算成功
        "after_wait": 1.0,
    },
]

# 失敗處理設定:
# SKIP_ON_FAIL: 搜尋或確認點擊失敗時，直接跳至下一個步驟繼續執行，整個流程到底後重頭開始 (不斷重複)
SKIP_ON_FAIL = True                 # 搜尋/點擊失敗直接跳下一步 (不斷重複循環)
DEFAULT_SEARCH_TIMEOUT = 3          # 每個圖片搜尋步驟預設最多搜尋 3 秒，超時即跳下一步
FAIL_STEP_DELAY = 0.5               # 失敗跳過時的緩衝等待秒數 (秒)
GROUP_RETRIES = 5                   # 同一組連續整組重做幾次還失敗, 就放棄這一輪回到最開頭 (SKIP_ON_FAIL=False 時生效)
LOOP_DELAY = 0.3                    # 每次搜尋間隔(秒)
SCALE_FULL_SCAN_INTERVAL = 8.0      # 有記住縮放比例時, 每隔幾秒才做一次全範圍比例掃描 (省時間)
MAX_STEP_TIME = 30                  # 單一步驟最多執行幾秒，超過視為卡住並強制跳過 (防卡死)
MAX_KEY_TIME = 120                  # 按鍵流程最多執行幾秒，超過就中止 (防卡死)
WATCH_IDLE_WARN = 45                # 流程幾秒沒有動作就記錄警告
WATCH_IDLE_RESTART = 180            # 流程幾秒完全沒動作就強制重啟 (防卡死)

# ---- 隨時可能出現的畫面 (step5 / step6) ----
# 每個搜尋迴圈都會先檢查這些圖片; 偵測到就按鍵 (空白鍵 / ESC, 在介面設定) 離開, 然後跳回指定步驟重新開始
STEP_INDEX = {"step1": 0, "step2": 1, "keys": 2, "step3": 3, "step4": 4}

WATCHES = [
    {
        "name": "鐵匠 技能 包包防卡 (step5)",
        "image": "step5.png",
        "threshold": 0.75,      # 相似度門檻
        "goto": "step3",        # 離開後, 回到 step3 開始
        "key_cfg": "step5_key",     # 按哪個鍵 / 按幾次 -> 在介面設定 (預設 空白鍵 1 次)
        "count_cfg": "step5_count",
        "after_wait": 1.5,      # 按鍵之後等待秒數
        "cooldown": 4.0,        # 幾秒內不重複觸發
        "max_retry": 3,         # 按鍵後畫面還在, 最多再按幾輪
        "scale_range": (0.6, 1.4),
    },
    {
        "name": "死亡畫面 (step6)",
        "image": "step6.png",
        "threshold": 0.75,
        "goto": "step1",        # 離開後, 回到 step1 開始
        "key_cfg": "step6_key",     # 預設 ESC 1 次
        "count_cfg": "step6_count",
        "after_wait": 2.0,
        "cooldown": 5.0,
        "max_retry": 3,
        "scale_range": (0.6, 1.4),
        "count_death": True,    # 觸發時「死亡次數」+1
    },
]

# 後台靜默模式 (確保自動操作完全不干擾您使用電腦或瀏覽其他網頁):
# True  = 後台模式 (使用 PostMessage，不移動滑鼠、不搶焦點、不干擾其他網頁操作)
# False = 前台模式 (移動真實滑鼠游標)
BACKGROUND_MODE = True              # 預設啟用後台模式 (不影響其他網頁使用)
RESTORE_CURSOR = True               # 若切換前台點擊，點擊後立刻把滑鼠復原回原位置
# ================================================================

# ---------------- 按鍵流程設定 (自動儲存 / 自動讀取) ----------------
# APP_DIR / CONFIG_PATH 已在檔案最上方定義 (含錯誤紀錄用得到)
KEY_LABELS = {"tab": "Tab", "enter": "Enter", "wait": "等待(秒)"}
KEY_CHOICES = [("space", "空白鍵"), ("esc", "ESC")]      # step5 / step6 / 防卡 可選的按鍵


def key_label(k):
    return {**dict(KEY_CHOICES), "tab": "Tab", "enter": "Enter"}.get(k, str(k))


KEY_ALIASES = {"空白": "space", "空白鍵": "space", "空格": "space", "spacebar": "space",
               "escape": "esc", "return": "enter", "tab鍵": "tab"}


def normalize_key(v):
    """副手戰吼的按鍵: 不分大小寫, 也接受 空白鍵 / Escape / Return 這類寫法"""
    v = str(v).strip().lower()
    return KEY_ALIASES.get(v, v)


def clean_key(v, default):
    v = str(v).strip().lower()
    return v if v in dict(KEY_CHOICES) else default
IMG2_VARIANTS = ["A1", "A2", "A3", "A4", "A5"]      # 第二張圖的 5 種狀況

DEFAULT_CONFIG = {
    # 流程由上往下執行; count 對 wait 代表秒數
    "key_sequence": [
        {"key": "tab", "count": 3},
        {"key": "enter", "count": 1},
        {"key": "tab", "count": 5},
    ],
    "key_interval": 0.30,        # 每次按鍵之間的間隔(秒)
    "key_hold": 0.08,            # 每次按鍵的按住時間(秒)
    "delay_after_img2": 2.0,     # 圖片 2 點擊成功後, 等幾秒才開始按鍵
    "img2_variant": "A1",        # 今天要搜尋的第二張圖種類
    "anti_stuck_enabled": True,  # 防卡機制: 太久沒通關就按 ESC
    "anti_stuck_minutes": 10.0,  # 幾分鐘沒有成功通關 (+1) 就按 ESC (要比正常一局的時間長)
    "idle_report_minutes": 5.0,  # 閒置幾分鐘沒通關就 DC 回報 (TAG 你), 0 = 關閉
    "anti_stuck_key": "space",   # 防卡要按的鍵: space / esc
    "anti_stuck_count": 1,       # 防卡每次按幾下
    "step5_key": "space",        # step5 (鐵匠 技能 包包防卡) 觸發後按的鍵 / 次數
    "step5_count": 1,
    "step6_key": "esc",          # step6 (死亡畫面) 觸發後按的鍵 / 次數
    "step6_count": 1,
    "offhand_key": "",           # 自動副手戰吼: 要按的鍵 (不預設)
    "offhand_count": 2,          # 每次快速按幾下
    "offhand_interval": 5.0,     # 每隔幾秒按一輪
}


def seq_item_text(it):
    if it["key"] == "wait":
        return f"等待 {it['count']} 秒"
    return f"{KEY_LABELS[it['key']]} × {it['count']}"


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data.get("key_sequence"), list):
            seq = []
            for it in data["key_sequence"]:
                if it.get("key") in KEY_LABELS:
                    seq.append({"key": it["key"], "count": max(1, int(it.get("count", 1)))})
            cfg["key_sequence"] = seq
        for k in ("key_interval", "key_hold", "delay_after_img2"):
            if k in data:
                cfg[k] = float(data[k])
        if data.get("img2_variant") in IMG2_VARIANTS:
            cfg["img2_variant"] = data["img2_variant"]
        if "anti_stuck_enabled" in data:
            cfg["anti_stuck_enabled"] = bool(data["anti_stuck_enabled"])
        if "anti_stuck_minutes" in data:
            cfg["anti_stuck_minutes"] = max(0.5, float(data["anti_stuck_minutes"]))
        if "idle_report_minutes" in data:
            cfg["idle_report_minutes"] = max(0.0, float(data["idle_report_minutes"]))
        for kn, dk in (("anti_stuck_key", "space"), ("step5_key", "space"), ("step6_key", "esc")):
            if kn in data:
                cfg[kn] = clean_key(data[kn], dk)
        for cn in ("anti_stuck_count", "step5_count", "step6_count", "offhand_count"):
            if cn in data:
                cfg[cn] = min(20, max(1, int(data[cn])))
        if "offhand_key" in data:
            cfg["offhand_key"] = normalize_key(data["offhand_key"])
        if "offhand_interval" in data:
            cfg["offhand_interval"] = min(3600.0, max(0.5, float(data["offhand_interval"])))
    except Exception:
        pass            # 檔案不存在或壞掉 -> 用預設值
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def find_window(keyword, exclude=()):
    """找標題含 keyword 的可見視窗; 排除自己(控制台)的視窗, 有多個就取畫面最大的"""
    found = []
    def cb(hwnd, _):
        if hwnd in exclude or not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if keyword.lower() in title.lower() and "控制台" not in title:
            l, t, r, b = win32gui.GetClientRect(hwnd)
            found.append((r * b, hwnd))
    win32gui.EnumWindows(cb, None)
    return max(found)[1] if found else None


def list_windows(exclude=()):
    """列出所有可見、有標題、有畫面大小的視窗 -> [(hwnd, title)]"""
    out = []
    def cb(hwnd, _):
        if hwnd in exclude or not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip()
        if not title or "控制台" in title:
            return
        l, t, r, b = win32gui.GetClientRect(hwnd)
        if r < 200 or b < 200:
            return
        out.append((hwnd, title))
    win32gui.EnumWindows(cb, None)
    return out


def client_region(hwnd):
    """回傳遊戲畫面(client area)在螢幕上的 left, top, width, height"""
    l, t, r, b = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (0, 0))
    return left, top, r - l, b - t


def load_template(name):
    path = find_image(name)
    data = np.fromfile(str(path), dtype=np.uint8)   # 支援中文路徑
    img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"讀不到圖片: {path}")
    return img


def window_responsive(hwnd, timeout_ms=500):
    """用 WM_NULL + 逾時測試視窗還有沒有回應.
    遊戲若卡住/沒回應, 就不送 PrintWindow, 否則 PrintWindow 會一起卡死整個程式."""
    try:
        result = ctypes.c_size_t()
        ok = ctypes.windll.user32.SendMessageTimeoutW(
            hwnd, 0, 0, 0, 0x0002, timeout_ms, ctypes.byref(result))   # SMTO_ABORTIFHUNG
        return bool(ok)
    except Exception:
        return True


def _print_window(hwnd, hdc, flags):
    """pywin32 的 win32gui 沒有 PrintWindow, 改用 ctypes 直接呼叫 user32.PrintWindow"""
    fn = ctypes.windll.user32.PrintWindow
    fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    return fn(hwnd, hdc, flags)


def window_unobstructed(hwnd):
    """遊戲視窗畫面是否完全沒被其他視窗蓋住 (只有沒被蓋住, 螢幕截圖才可信)"""
    try:
        if win32gui.IsIconic(hwnd):
            return False
        l, t, r, b = win32gui.GetClientRect(hwnd)
        if r <= 0 or b <= 0:
            return False
        root = ctypes.windll.user32.GetAncestor(hwnd, 2)       # GA_ROOT
        for fx, fy in ((0.5, 0.5), (0.1, 0.1), (0.9, 0.1), (0.1, 0.9), (0.9, 0.9), (0.5, 0.1), (0.5, 0.9), (0.1, 0.5), (0.9, 0.5)):
            sx, sy = win32gui.ClientToScreen(hwnd, (int(r * fx), int(b * fy)))
            h = win32gui.WindowFromPoint((sx, sy))
            if not h or ctypes.windll.user32.GetAncestor(h, 2) != root:
                return False
        return True
    except Exception:
        return False


class WindowGrabber:
    """後台截圖器：重複使用同一組 GDI 物件。

    舊版每張截圖都重新建立/釋放 DC 與 Bitmap，長時間執行容易耗盡 GDI 控制代碼，
    造成程式越跑越慢甚至突然消失；改成重複使用後不但穩定，截圖也快很多。
    """

    def __init__(self, hwnd):
        self.hwnd = hwnd
        self._hwnd_dc = None
        self._mfc_dc = None
        self._save_dc = None
        self._bmp = None
        self._size = (0, 0)
        self._mode = "client"
        self.fail_reason = ""

    def close(self):
        try:
            if self._save_dc is not None:
                self._save_dc.DeleteDC()
        except Exception:
            pass
        try:
            if self._mfc_dc is not None:
                self._mfc_dc.DeleteDC()
        except Exception:
            pass
        try:
            if self._bmp is not None:
                win32gui.DeleteObject(self._bmp.GetHandle())
        except Exception:
            pass
        try:
            if self._hwnd_dc is not None:
                win32gui.ReleaseDC(self.hwnd, self._hwnd_dc)
        except Exception:
            pass
        self._hwnd_dc = self._mfc_dc = self._save_dc = self._bmp = None
        self._size = (0, 0)

    def _prepare(self, w, h):
        if self._save_dc is not None and self._size == (w, h):
            return True
        self.close()
        try:
            import win32ui
            self._hwnd_dc = win32gui.GetDC(self.hwnd)
            self._mfc_dc = win32ui.CreateDCFromHandle(self._hwnd_dc)
            self._save_dc = self._mfc_dc.CreateCompatibleDC()
            self._bmp = win32ui.CreateBitmap()
            self._bmp.CreateCompatibleBitmap(self._mfc_dc, w, h)
            self._save_dc.SelectObject(self._bmp)
            self._size = (w, h)
            return True
        except Exception as e:
            self.close()
            log_event(f"截圖物件建立失敗: {e}")
            return False

    def _child_candidates(self, w, h):
        """主視窗底下, 畫面區大小與主視窗相同的子視窗 (有些遊戲的畫面畫在子視窗裡)"""
        out = []
        def cb(ch, _):
            try:
                l, t, r, b = win32gui.GetClientRect(ch)
                if abs((r - l) - w) <= 2 and abs((b - t) - h) <= 2:
                    out.append(ch)
            except Exception:
                pass
        try:
            win32gui.EnumChildWindows(self.hwnd, cb, None)
        except Exception:
            pass
        return out

    def _print_to_gray(self, target, hdc, w, h, flags, reasons):
        """對 target 視窗依序試 flags, 回傳整張灰階 (大小 w x h) 或 None"""
        for flag in flags:
            try:
                ok = _print_window(target, hdc, flag)
            except Exception as e:
                reasons.append(f"{target:#x}/f{flag}:例外{e}")
                continue
            if not ok:
                reasons.append(f"{target:#x}/f{flag}:回傳失敗")
                continue
            data = self._bmp.GetBitmapBits(True)
            img = np.frombuffer(data, dtype=np.uint8).reshape((h, w, 4))
            gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
            if gray.size and gray.any():
                return gray
            reasons.append(f"{target:#x}/f{flag}:全黑")
        return None

    def _grab_mode(self, mode, w, h, reasons):
        """mode='client': PW_CLIENTONLY 只截畫面區 (座標與點擊一致)
           mode='window': 截整個視窗 (含標題列/邊框) 再裁出畫面區 -- 不裁的話, 圖會整個偏移,
                          找得到按鈕但點的位置歪掉"""
        if mode == "client":
            if not self._prepare(w, h):
                return None
            return self._print_to_gray(self.hwnd, self._save_dc.GetSafeHdc(), w, h, (3,), reasons)
        wl, wt, wr, wb = win32gui.GetWindowRect(self.hwnd)
        ww, wh = wr - wl, wb - wt
        if ww <= 0 or wh <= 0 or not self._prepare(ww, wh):
            return None
        full = self._print_to_gray(self.hwnd, self._save_dc.GetSafeHdc(), ww, wh, (2, 0), reasons)
        if full is None:
            return None
        cx, cy = win32gui.ClientToScreen(self.hwnd, (0, 0))
        ox, oy = cx - wl, cy - wt
        crop = full[oy:oy + h, ox:ox + w]
        if crop.shape != (h, w):
            reasons.append(f"裁切失敗 偏移({ox},{oy}) 視窗{ww}x{wh} 畫面{w}x{h}")
            return None
        return np.ascontiguousarray(crop)

    def grab(self):
        """回傳灰階畫面 (numpy), 大小 = 遊戲畫面區, 座標與點擊一致; 失敗回 None, 原因存在 self.fail_reason"""
        self.fail_reason = ""
        try:
            if not win32gui.IsWindow(self.hwnd):
                self.fail_reason = "視窗不存在"
                return None
            if win32gui.IsIconic(self.hwnd):
                self.fail_reason = "遊戲視窗已最小化 (最小化時 Windows 不會繪製畫面, 請改成被其他視窗蓋住即可)"
                return None
            if not window_responsive(self.hwnd):
                self.fail_reason = "遊戲視窗沒有回應 (SendMessageTimeout)"
                return None
            l, t, r, b = win32gui.GetClientRect(self.hwnd)
            w, h = r - l, b - t
            if w <= 0 or h <= 0:
                self.fail_reason = f"畫面大小異常 {w}x{h}"
                return None
            reasons = []
            for mode in (self._mode, "window" if self._mode == "client" else "client"):
                gray = self._grab_mode(mode, w, h, reasons)
                if gray is not None:
                    if mode != self._mode:
                        self._mode = mode
                        log_event(f"截圖模式改為 {mode}")
                    return gray
            for ch in self._child_candidates(w, h)[:4]:         # 主視窗截不到, 改試同大小的子視窗
                if not self._prepare(w, h):
                    break
                gray = self._print_to_gray(ch, self._save_dc.GetSafeHdc(), w, h, (3, 2, 0), reasons)
                if gray is not None:
                    self._mode = "client"
                    return gray
            self.fail_reason = "; ".join(reasons[:8]) or "未知"
            return None
        except Exception as e:
            self.fail_reason = f"例外: {e}"
            self.close()
            return None


def click_window(hwnd, client_x, client_y, screen_x, screen_y, background=True):
    """點擊視窗：後台模式直接發送 WM_LBUTTONDOWN/UP 給目標視窗，不移動實體滑鼠游標，不搶焦點"""
    if background:
        lParam = win32api.MAKELONG(int(client_x), int(client_y))
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lParam)
        time.sleep(0.02)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lParam)
        time.sleep(0.05)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lParam)
    else:
        orig = win32api.GetCursorPos()
        win32api.SetCursorPos((int(screen_x), int(screen_y)))
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        if RESTORE_CURSOR:
            win32api.SetCursorPos(orig)


VK = {"shift": 0x10, "ctrl": 0x11, "alt": 0x12, "tab": 0x09, "enter": 0x0D,
      "esc": 0x1B, "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27}
for _i in range(1, 13):
    VK[f"f{_i}"] = 0x6F + _i


def _vk(name):
    name = name.lower()
    if name in VK:
        return VK[name]
    if len(name) == 1:
        return ord(name.upper())
    raise ValueError(f"不支援的按鍵: {name}")


# ---------------- 後台按鍵 (PostMessage) ----------------
# 只把按鍵訊息送給鎖定的遊戲視窗: 不搶焦點、不動滑鼠、不影響你使用其他程式.
# 全程「只用 PostMessage」, 不使用 keybd_event / SendInput 這類全域按鍵,
# 所以 step5 / step6 的 ESC 不會被送到瀏覽器、記事本等其他程式.
# (Shift 這類修飾鍵在後台無法被系統認到, 所以流程只提供 Tab / Enter)
def post_key(hwnd, name, hold=0.08):
    vk = _vk(name)
    scan = win32api.MapVirtualKey(vk, 0)
    win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, 1 | (scan << 16))
    time.sleep(hold)
    win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, 1 | (scan << 16) | (1 << 30) | (1 << 31))


def post_key_down(hwnd, name, repeat=False):
    """只送出「按下」訊息 (給 WASD 按住移動用, 同樣是後台 PostMessage)
    repeat=True 表示系統自動重複 (lParam bit 30), 模擬「按住不放」"""
    vk = _vk(name)
    scan = win32api.MapVirtualKey(vk, 0)
    lparam = 1 | (scan << 16)
    if repeat:
        lparam |= 1 << 30
    win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, lparam)


def post_key_up(hwnd, name):
    """只送出「放開」訊息 (給 WASD 按住移動用, 同樣是後台 PostMessage)"""
    vk = _vk(name)
    scan = win32api.MapVirtualKey(vk, 0)
    win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, 1 | (scan << 16) | (1 << 30) | (1 << 31))


def key_targets(hwnd):
    """要送按鍵的視窗清單: 先送主視窗, 沒反應再改成客戶區的子視窗 (仍是後台送出).
    有些遊戲的輸入視窗是子視窗, 只送主視窗會沒反應; 但絕對不會送到其他程式."""
    out = [hwnd]
    try:
        l, t, r, b = win32gui.GetClientRect(hwnd)
        child = win32gui.RealChildWindowFromPoint(hwnd, ((r - l) // 2, (b - t) // 2))
        if child and child != hwnd and child not in out:
            out.append(child)
    except Exception:
        pass
    return out


def fake_focus(hwnd):
    """用訊息讓遊戲以為自己仍在前景 (有些遊戲被切到背景就暫停, 畫面會整個停住)"""
    try:
        win32gui.PostMessage(hwnd, win32con.WM_ACTIVATEAPP, 1, 0)
        win32gui.PostMessage(hwnd, win32con.WM_ACTIVATE, 1, 0)        # WA_ACTIVE
        win32gui.PostMessage(hwnd, win32con.WM_SETFOCUS, 0, 0)
    except Exception:
        pass


# ============================ Discord 通知 ============================
DEFAULT_DISCORD = {
    "_說明": "webhook_url: Discord 頻道 > 編輯頻道 > 整合 > Webhook 複製網址; mention_user_id: 你的使用者ID (開發者模式 > 右鍵頭像 > 複製使用者ID), 卡死時會 TAG 你",
    "enabled": True,
    "webhook_url": "",
    "mention_user_id": "",
    "mention_role_id": "",
    "username": "D2Auto",
    "stuck_seconds": 60,         # 遊戲凍結 / 無回應 / 截不到畫面 持續幾秒就通知
    "menu_stuck_seconds": 0,     # 選單空轉幾秒沒進展就通知 (0 = 關閉; 戰鬥本來就 3~6 分鐘不用動作, 開了容易誤報)
    "round_timeout_minutes": 5,  # (已改由 GUI「閒置沒通關DC回報」設定, 這個值只在讀不到 GUI 設定時當備用)
    "battle_max_minutes": 20,    # 戰鬥中不算卡死, 但超過幾分鐘還沒結束就通知 (0 = 不限)
    "battle_motion_ratio": 0.05, # 沒有戰鬥階段資訊時 (剛啟動), 畫面動態超過這比例就當作在戰鬥
    "repeat_minutes": 10,        # 還在卡死時, 每幾分鐘再 TAG 一次 (0 = 只通知一次)
    "notify_stuck": True,        # 卡死 (TAG 你)
    "notify_recovered": True,    # 恢復正常
    "notify_clear": True,        # 每通關一次記錄 (靜音, 附總通關數)
}


class DiscordNotifier:
    """背景執行緒送 Discord Webhook, 網路慢或斷線都不會卡住自動化流程"""

    def __init__(self):
        self._lock = threading.Lock()
        self._q = queue.Queue(maxsize=50)
        self._cfg = dict(DEFAULT_DISCORD)
        self._mtime = None
        self.total = self._load_total()
        self.cfg()
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- 設定 (改檔案後自動生效, 不用重開程式) ----
    def cfg(self):
        try:
            if not DISCORD_PATH.exists():
                DISCORD_PATH.write_text(json.dumps(DEFAULT_DISCORD, ensure_ascii=False, indent=2), encoding="utf-8")
            mt = DISCORD_PATH.stat().st_mtime
            if mt != self._mtime:
                txt = DISCORD_PATH.read_text(encoding="utf-8-sig").lstrip()
                data, end = json.JSONDecoder().raw_decode(txt)      # 容許 } 後面不小心留下的文字
                if txt[end:].strip():
                    log_event("discord_config.json 的 } 後面有多餘文字, 已忽略 (建議刪掉)")
                c = dict(DEFAULT_DISCORD)
                c.update({k: data[k] for k in DEFAULT_DISCORD if k in data})
                self._cfg, self._mtime = c, mt
        except Exception as e:
            log_event(f"discord_config.json 讀取失敗: {e}")
        return self._cfg

    def ready(self):
        c = self.cfg()
        return bool(c["enabled"]) and str(c["webhook_url"]).startswith("http")

    def status_text(self):
        """啟動 / 測試時用: 說明 Discord 通知目前能不能用"""
        c = self.cfg()
        url = str(c["webhook_url"]).strip()
        if not c["enabled"]:
            return "enabled 是 false"
        if not url:
            return "webhook_url 是空的 (或 JSON 格式錯誤導致讀不到, 請看上面的讀取失敗紀錄)"
        if not url.startswith("https://discord.com/api/webhooks/") and not url.startswith("https://discordapp.com/api/webhooks/"):
            return "webhook_url 格式不對, 應以 https://discord.com/api/webhooks/ 開頭"
        if len(url.split("/")) < 7 or len(url.rsplit("/", 1)[-1]) < 40:
            return "webhook_url 看起來不完整 (後面的 token 太短, 請重新複製整串網址)"
        if not str(c["mention_user_id"]).strip().isdigit():
            return "mention_user_id 要是純數字"
        return "OK"

    # ---- 計數 (總通關 / 今日通關 / 今日死亡; 換日自動重置今日, 存檔後重開程式不歸零) ----
    def _load_total(self):
        self.today, self.today_clear, self.today_death = time.strftime("%Y-%m-%d"), 0, 0
        try:
            d = json.loads(CLEAR_PATH.read_text(encoding="utf-8"))
            if d.get("date") == self.today:
                self.today_clear = int(d.get("today_clear", 0))
                self.today_death = int(d.get("today_death", 0))
            return int(d.get("total", 0))
        except Exception:
            return 0

    def _roll_day(self):
        day = time.strftime("%Y-%m-%d")
        if day != self.today:                       # 換日: 今日數字重刷
            self.today, self.today_clear, self.today_death = day, 0, 0

    def _save_counts(self):
        try:
            CLEAR_PATH.write_text(json.dumps({"total": self.total, "date": self.today,
                                              "today_clear": self.today_clear,
                                              "today_death": self.today_death}), encoding="utf-8")
        except Exception:
            pass

    def today_total(self):
        with self._lock:
            self._roll_day()
            return self.today_clear

    def add_clear(self):
        with self._lock:
            self._roll_day()
            self.total += 1
            self.today_clear += 1
            self._save_counts()
            return self.total

    def add_death(self):
        with self._lock:
            self._roll_day()
            self.today_death += 1
            self._save_counts()

    # ---- 發送 ----
    def send(self, text, ping=False, silent=False):
        if not self.ready():
            return
        try:
            self._q.put_nowait((text, ping, silent))
        except queue.Full:
            pass

    def _post(self, text, ping, silent):
        c = self.cfg()
        users, roles, tags = [], [], []
        if ping:
            if str(c["mention_user_id"]).strip():
                users.append(str(c["mention_user_id"]).strip()); tags.append(f"<@{users[-1]}>")
            if str(c["mention_role_id"]).strip():
                roles.append(str(c["mention_role_id"]).strip()); tags.append(f"<@&{roles[-1]}>")
        body = {"content": (" ".join(tags) + " " + text).strip(),
                "username": str(c["username"]) or "D2Auto",
                "allowed_mentions": {"parse": [], "users": users, "roles": roles}}
        if silent:
            body["flags"] = 4096                    # SUPPRESS_NOTIFICATIONS
        data = json.dumps(body).encode("utf-8")
        for _ in range(3):
            req = urllib.request.Request(str(c["webhook_url"]), data=data, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": "D2Auto/1.0"})
            try:
                urllib.request.urlopen(req, timeout=10).read()
                return True
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    try:
                        time.sleep(min(10.0, float(json.loads(e.read()).get("retry_after", 2))))
                        continue
                    except Exception:
                        time.sleep(2); continue
                try:
                    body_txt = e.read().decode("utf-8", "ignore")[:200]
                except Exception:
                    body_txt = ""
                log_event(f"Discord 發送失敗 HTTP {e.code} {body_txt}")
                return False
            except Exception as e:
                log_event(f"Discord 發送失敗: {e}")
                time.sleep(2)
        return False

    def _loop(self):
        while True:
            text, ping, silent = self._q.get()
            try:
                self._post(text, ping, silent)
            except Exception as e:
                log_event(f"Discord 執行緒錯誤: {e}")

    # ---- 事件 ----
    @staticmethod
    def _fmt_round(sec):
        """本局時間: 不到 1 小時 M:SS (例 6:57), 1 小時以上 H:MM:SS"""
        h, rem = divmod(max(0, int(sec)), 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def notify_clear(self, session_rounds, elapsed_sec, round_sec=0):
        """通關｜執行第 N 關｜今日總數 N｜死亡 N
        本局時間M:SS｜已執行 HH:MM:SS   (本局時間 = 這次通關 - 上次通關, 剛開始則從 0:00 起算)"""
        self.add_clear()
        if self.cfg()["notify_clear"]:
            h, rem = divmod(int(elapsed_sec), 3600)
            m, sec = divmod(rem, 60)
            self.send(f"✅ 通關｜執行第 {session_rounds:,} 關｜今日總數 {self.today_clear:,}｜死亡 {self.today_death:,}\n"
                      f"本局時間{self._fmt_round(round_sec)}｜已執行 {h:02d}:{m:02d}:{sec:02d}", silent=True)


class FreezeMonitor:
    """判斷流程是否卡住. 不看「畫面是否靜止」(選單有火焰 / 河流一直在動), 改看「流程有沒有進展」.

    進展 = 完成關鍵步驟: 按鍵流程做完 (進入戰鬥) / 3. 離開紅門 / 4. 結束這局 / 處理死亡或鐵匠畫面.
    (1、2 步驟點得到不算進展, 否則 1 一直成功、2 一直失敗的空轉會抓不到)
    階段:
      戰鬥中 (按鍵流程做完 -> 點到離開紅門): 畫面一直在變就不算卡死, 超過 battle_max_minutes 才通知
      其他 (選單 / 傳點 / 選關): 超過 stuck_seconds 沒有進展就通知, 不管畫面有沒有在動
    另外三種硬故障也會通知: 畫面完全靜止且沒進展 (凍結 / 被背景暫停)、視窗無回應、連續截不到畫面.
    剛啟動還不知道階段時, 用「畫面動態量」猜是不是戰鬥 (戰鬥全畫面都在動, 選單動畫只佔一小塊)."""
    SIZE = (64, 36)

    def __init__(self, notifier):
        self.n = notifier
        self.battle_thr = 0.05
        self.reset(None, None)

    def reset(self, hwnd, worker):
        now = time.time()
        self.hwnd, self.worker = hwnd, worker
        self.prev = None
        self.last_motion = self.last_grab = self.last_progress = self.last_battle_like = now
        self.motion = 0.0                  # 畫面動態量 (變化格數比例, 平滑後)
        self.phase = None                  # None / "battle" / "menu"
        self.phase_since = now
        self.since = None
        self.last_alert = 0.0
        self.nudged = False
        self.hung_since = None
        self._last_dbg = now
        self.last_round = now              # 上次完成一局的時間
        self.round_alert = 0.0

    def _round_timeout_min(self, c):
        """閒置幾分鐘沒通關就回報: 以 GUI 設定 (idle_report_minutes) 為準, 讀不到才用 discord_config.json"""
        try:
            return max(0.0, float(self.worker.cfg["idle_report_minutes"]))
        except Exception:
            return max(0.0, float(c["round_timeout_minutes"]))

    # ---- worker 執行緒呼叫 ----
    def feed(self, gray):
        try:
            small = cv2.resize(gray, self.SIZE, interpolation=cv2.INTER_AREA)
            now = time.time()
            if self.prev is not None:
                cnt = int((cv2.absdiff(small, self.prev) > 10).sum())
                if cnt >= 4:
                    self.last_motion = now
                self.motion = self.motion * 0.8 + 0.2 * (cnt / float(self.SIZE[0] * self.SIZE[1]))
                if self.motion >= self.battle_thr:
                    self.last_battle_like = now
            self.prev = small
            self.last_grab = now
        except Exception:
            pass

    def touch(self):
        """做長流程 (按鍵流程) 前先重新計時, 不改階段"""
        self.last_progress = time.time()

    def progress(self, step):
        """一個步驟成功時呼叫; 只有關鍵步驟才算進展"""
        now = time.time()
        if step.get("starts_battle"):
            self.phase, self.phase_since, self.last_progress = "battle", now, now
        elif step.get("ends_battle") or step.get("count_round"):
            self.phase, self.phase_since, self.last_progress = "menu", now, now
            if step.get("count_round"):                          # 成功點到「再來一局」且關卡數 +1 才算完成一局
                self.last_round, self.round_alert = now, 0.0

    # ---- 介面計時器每秒呼叫 ----
    def _reason(self, now, S, c):
        try:
            hung = bool(ctypes.windll.user32.IsHungAppWindow(ctypes.c_void_p(self.hwnd)))
        except Exception:
            hung = False
        if hung:
            self.hung_since = self.hung_since or now
            if now - self.hung_since >= min(S, 15):
                return "遊戲視窗無回應 (Not Responding)", "hard"
        else:
            self.hung_since = None
        if now - self.last_motion >= S and now - self.last_progress >= S:
            return f"畫面 {int(now - self.last_motion)} 秒完全沒變化, 流程也沒有進展 (遊戲凍結 / 被背景暫停)", "static"
        if now - self.last_grab >= S:
            why = getattr(getattr(self.worker, "grabber", None), "fail_reason", "")
            return f"連續 {int(now - self.last_grab)} 秒截不到畫面 ({why or '未知'})", "hard"

        idle = now - self.last_progress
        bmax = float(c["battle_max_minutes"]) * 60
        if self.phase == "battle":
            if bmax > 0 and self._round_timeout_min(c) <= 0 and now - self.phase_since >= bmax and idle >= S:
                return f"戰鬥已超過 {int((now - self.phase_since) / 60)} 分鐘還沒結束 (找不到「3. 搜尋離開紅門」?)", "flow"
            return None, ""
        if self.phase is None and now - self.last_battle_like < 10:
            return None, ""                         # 剛啟動, 畫面動態大 = 當作在戰鬥
        M = float(c["menu_stuck_seconds"])
        if M > 0 and idle >= M:
            return f"流程已 {int(idle)} 秒沒有進展 (卡在選單 / 一直點不到按鈕), 畫面仍有動態", "flow"
        return None, ""

    def check(self, running):
        now = time.time()
        if not running or self.hwnd is None or not win32gui.IsWindow(self.hwnd):
            self.last_motion = self.last_grab = self.last_progress = self.last_battle_like = now
            self.since = None
            self.last_round = now
            return
        c = self.n.cfg()
        S = max(10.0, float(c["stuck_seconds"]))
        self.battle_thr = float(c["battle_motion_ratio"])

        if now - self._last_dbg >= 60:             # 每分鐘記一筆, 方便校正 battle_motion_ratio
            self._last_dbg = now
            log_event(f"監看: 階段={self.phase} 畫面動態={self.motion * 100:.1f}% 距上次進展 {int(now - self.last_progress)} 秒")

        # 畫面完全不動又沒進展到 40% 門檻: 先送「假前景」訊息推一把
        still = min(now - self.last_motion, now - self.last_progress)
        if still < 5:
            self.nudged = False
        elif still >= S * 0.4 and not self.nudged:
            self.nudged = True
            fake_focus(self.hwnd)
            log_event(f"畫面 {int(still)} 秒沒動, 已送出假前景訊息")

        # 超過 round_timeout_minutes 沒有「成功點到再來一局 (關卡數 +1)」-> TAG (與卡住通知各自獨立)
        rt = self._round_timeout_min(c) * 60
        if rt > 0 and c["notify_stuck"] and now - self.last_round >= rt:
            rep_s = float(c["repeat_minutes"]) * 60
            if self.round_alert == 0 or (rep_s > 0 and now - self.round_alert >= rep_s):
                self.round_alert = now
                gap = int(now - self.last_round)
                ph = {"battle": "戰鬥中", "menu": "選單 / 準備中"}.get(self.phase, "未知")
                step = getattr(self.worker, "cur_step", "") or "—"
                self.n.send(f"⏱ **已 {gap // 60} 分 {gap % 60} 秒沒有完成一局** (上限 {rt / 60:g} 分鐘)\n"
                            f"階段: {ph}｜目前步驟: {step}\n今日總通關: {self.n.today_total():,}｜{time.strftime('%m-%d %H:%M:%S')}",
                            ping=True)
                log_event(f"超過 {rt / 60:g} 分鐘沒有完成一局")

        reason, kind = self._reason(now, S, c)
        if reason:
            if self.since is None:
                self.since = now - S
                log_event(f"偵測到卡住: {reason}")
            rep = float(c["repeat_minutes"]) * 60
            if c["notify_stuck"] and (self.last_alert == 0 or (rep > 0 and now - self.last_alert >= rep)):
                self.last_alert = now
                w = self.worker
                step = getattr(w, "cur_step", "") or "—"
                extra = ""
                if kind == "flow" and getattr(w, "cur_thr", None):
                    extra = f"\n最近一次比對相似度 {getattr(w, 'last_best', 0):.2f} (門檻 {w.cur_thr})"
                elif kind == "static" and not self._visible():
                    extra = "\n⚠ 遊戲被蓋住且不在前景, 可能被遊戲自己暫停 (請檢查遊戲內「背景執行」設定)"
                self.n.send(f"🚨 **流程卡住**\n原因: {reason}\n目前步驟: {step}{extra}\n"
                            f"今日總通關: {self.n.today_total():,}｜{time.strftime('%m-%d %H:%M:%S')}", ping=True)
        elif self.since is not None:
            if self.last_alert and c["notify_recovered"]:
                self.n.send(f"🟢 已恢復正常 (約卡了 {int(now - self.since)} 秒)｜今日總通關 {self.n.today_total():,}")
            self.since, self.last_alert = None, 0.0

    def _visible(self):
        """遊戲視窗是前景或沒被蓋住 -> True"""
        try:
            return win32gui.GetForegroundWindow() == self.hwnd or window_unobstructed(self.hwnd)
        except Exception:
            return True


class Worker(QThread):
    sig_status = Signal(str, str, str)   # 目前搜尋名稱, 圖片檔名, 下一步
    sig_log = Signal(str, str)           # 內容, 狀態(成功/失敗/等待中)
    sig_result = Signal(bool, str)
    sig_round = Signal()                 # 完成一個關卡
    sig_death = Signal()                 # 偵測到死亡畫面
    sig_watch = Signal(str, str)         # step5/step6 監看狀態 (圖片檔名, 狀態文字)

    def __init__(self, hwnd, skip_on_fail=SKIP_ON_FAIL, background_mode=BACKGROUND_MODE, cfg=None):
        super().__init__()
        self.hwnd = hwnd
        self.cfg = cfg if cfg is not None else load_config()   # 按鍵流程設定 (與介面共用, 改了下一輪生效)
        self.skip_on_fail = skip_on_fail
        self.background_mode = background_mode
        self.scale_cache = {}           # 各圖片上次成功的縮放比例
        self.global_scale = None
        self.last_best = 0.0
        self.stop_ev = threading.Event()
        self.run_ev = threading.Event()
        self.run_ev.set()
        self.beat = time.time()         # 心跳：介面用來判斷流程是否卡死
        self.sct = None
        self.grabber = None
        self.pending_jump = None        # 中斷後要跳回的步驟 (由主迴圈套用)
        self._tpl_scaled = {}           # (圖名, 比例) -> 已縮放模板, 避免每次重算
        self._last_full = {}            # 圖名 -> 上次做「全範圍縮放」搜尋的時間
        self._watch_cd = {}             # 圖名 -> 冷卻結束時間
        self._watch_tpl = {}
        self._watch_steps = []
        self._paused_logged = False
        self.monitor = None             # FreezeMonitor (由介面指派)
        self.cur_step = ""
        self.cur_thr = None
        self.anti_ref = time.time()     # 防卡計時起點: 上次通關 / 上次防卡 / 開始 / 暫停結束
        self.anti_count = 0             # 本次執行防卡觸發次數
        self._acting = False            # True = 步驟已成功 (找到圖) 正在點擊 / 正在跑按鍵流程; 找圖搜尋中是 False
        self._op_depth = 0              # >0 = 正在處理 step5 / step6 或防卡送鍵

    # ---------- 執行狀態 ----------
    def _beat(self):
        self.beat = time.time()

    def pause(self, on):
        if on:
            self.run_ev.clear()
        else:
            self.run_ev.set()

    def stop(self):
        self.stop_ev.set()
        self.run_ev.set()

    def _wait_if_paused(self):
        """暫停時每 0.2 秒檢查一次停止旗標, 不會像以前一樣整個卡住"""
        if self.run_ev.is_set():
            return not self.stop_ev.is_set()
        if not self._paused_logged:
            self._paused_logged = True
            self._pause_t0 = time.time()
            self.sig_log.emit("已暫停，等待繼續", "等待中")
        while not self.stop_ev.is_set():
            if self.run_ev.wait(0.2):
                self._paused_logged = False
                self.anti_ref += time.time() - getattr(self, "_pause_t0", time.time())   # 暫停的時間不算進防卡
                return True
            self._beat()
        return False

    def _sleep(self, sec):
        end = time.time() + sec
        while time.time() < end and not self.stop_ev.is_set():
            self._beat()
            time.sleep(0.05)

    def _alive(self):
        if win32gui.IsWindow(self.hwnd):
            return True
        self.sig_log.emit("遊戲視窗已關閉，流程停止", "失敗")
        self.sig_result.emit(False, "遊戲視窗已關閉")
        log_event("遊戲視窗已關閉，流程停止")
        self.stop_ev.set()
        return False

    # ---------- 截圖 ----------
    def _grab(self, sct=None):
        """所有截圖都經過這裡, 順便餵給卡死偵測"""
        r = self._grab_raw(sct)
        if r is not None and self.monitor is not None:
            self.monitor.feed(r[0])
        return r

    def _grab_raw(self, sct=None):
        """回傳 (gray, left, top) 或 None"""
        if not self._alive():
            return None
        if self.background_mode and self.grabber is not None:
            img = self.grabber.grab()          # 後台截圖：被其他視窗遮住也抓得到
            if img is None and win32gui.IsWindow(self.hwnd):
                self.grabber.close()           # 重建 GDI 物件再試一次
                img = self.grabber.grab()
            if img is not None:
                left, top, _, _ = client_region(self.hwnd)
                return img, left, top
            # 後台截圖失敗: 只有遊戲畫面完全沒被蓋住時才可改抓螢幕, 否則會抓到蓋在上面的視窗而點錯
            if not window_unobstructed(self.hwnd):
                now = time.time()
                if now - getattr(self, "_last_grab_warn", 0) > 30:
                    self._last_grab_warn = now
                    log_event("後台截圖失敗且視窗被遮住, 略過這一幀 | 原因: " + str(getattr(self.grabber, "fail_reason", "")))
                return None
        sct = sct or self.sct
        if sct is None:
            return None
        left, top, w, h = client_region(self.hwnd)
        if w <= 0 or h <= 0:
            return None
        try:
            shot = np.array(sct.grab({"left": left, "top": top, "width": w, "height": h}))
        except Exception:
            return None
        return cv2.cvtColor(shot, cv2.COLOR_BGRA2GRAY), left, top

    # ---------- 比對 ----------
    @staticmethod
    def _full_match(gray, tpl, thr):
        res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
        _, val, _, loc = cv2.minMaxLoc(res)
        return val >= thr, val, loc[0] + tpl.shape[1] / 2, loc[1] + tpl.shape[0] / 2

    @staticmethod
    def _partial_match(gray, tpl, step, thr):
        """把模板切成多個小區塊分別比對, 只要有幾塊(min_hits)對得上且位置一致就算找到.
        物件被部分遮住時, 沒被遮到的區塊仍然能對上.
        先用整張模板快速掃一次, 分數太低就代表畫面上沒有這個東西 -> 省下 9 次區塊比對."""
        res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
        _, full_val, _, _ = cv2.minMaxLoc(res)
        if full_val < step.get("prefilter", 0.20):
            return False, full_val, 0, 0
        gy, gx = step.get("grid", (3, 3))
        th, tw = tpl.shape[:2]
        ph, pw = th // gy, tw // gx
        pthr = step.get("patch_threshold", thr)
        tol = step.get("tolerance", 15)
        hits, top_val = [], 0.0
        for iy in range(gy):
            for ix in range(gx):
                py, px = iy * ph, ix * pw
                patch = tpl[py:py + ph, px:px + pw]
                if patch.size == 0 or patch.std() < 8:   # 太平淡的區塊(純色)不可靠, 略過
                    continue
                res = cv2.matchTemplate(gray, patch, cv2.TM_CCOEFF_NORMED)
                _, val, _, loc = cv2.minMaxLoc(res)
                top_val = max(top_val, val)
                if val >= pthr:
                    hits.append((val, loc[0] - px + tw / 2, loc[1] - py + th / 2))
        best = []
        for h in hits:
            group = [g for g in hits if abs(g[1] - h[1]) <= tol and abs(g[2] - h[2]) <= tol]
            if len(group) > len(best):
                best = group
        if len(best) >= step.get("min_hits", 2):
            return (True, sum(g[0] for g in best) / len(best),
                    sum(g[1] for g in best) / len(best),
                    sum(g[2] for g in best) / len(best))
        return False, top_val, 0, 0

    def _scaled_tpl(self, key, tpl, sc):
        """縮放後的模板只算一次並記住 (原本每次搜尋都重算, 很吃 CPU)"""
        k = (key, round(sc, 3))
        t = self._tpl_scaled.get(k)
        if t is None:
            if len(self._tpl_scaled) > 400:
                self._tpl_scaled.clear()
            t = tpl if sc == 1.0 else cv2.resize(tpl, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
            self._tpl_scaled[k] = t
        return t

    def _try(self, gray, tpl, step, thr, sc, key):
        t = self._scaled_tpl(key, tpl, sc)
        if t.shape[0] > gray.shape[0] or t.shape[1] > gray.shape[1]:
            return (False, 0.0, 0.0, 0.0, sc)
        if step.get("match_mode") == "partial":
            r = self._partial_match(gray, t, step, thr)
        else:
            r = self._full_match(gray, t, thr)
        return r + (sc,)

    @staticmethod
    def _downscale_for_search(gray, tpl):
        """大畫面先縮一半再比對 (速度快約 4 倍); 小圖不縮, 避免細節糊掉"""
        h, w = gray.shape[:2]
        if h * w < 900_000 or min(tpl.shape[:2]) < 28:
            return gray, 1.0
        return cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA), 0.5

    def _candidate_scales(self, key, step, fast):
        """優先試上次成功的縮放比例, 久久才做一次全範圍掃描 -> 平常快很多"""
        lo, hi = step.get("scale_range", (1.0, 1.0))
        if lo >= hi:
            return [1.0]
        hint = self.scale_cache.get(key) or self.global_scale
        if hint is None:
            return [round(x, 3) for x in np.arange(lo, hi + 1e-9, 0.05)]
        near = [round(hint + d, 3) for d in (0, -0.01, 0.01, -0.02, 0.02)]
        near = [s for s in near if lo - 0.05 <= s <= hi + 0.05]
        # 這張圖自己還沒記住比例時, 不能只信別張圖的比例, 還是要經常全範圍找一次
        own = key in self.scale_cache
        if fast and own:
            return near or [float(hint)]
        interval = SCALE_FULL_SCAN_INTERVAL if own else 2.0
        if time.time() - self._last_full.get(key, 0.0) < interval:
            return near or [float(hint)]
        self._last_full[key] = time.time()
        rest = [round(x, 3) for x in np.arange(lo, hi + 1e-9, 0.05)
                if all(abs(x - s) > 0.011 for s in near)]
        return (near + rest) or [float(hint)]

    def _refine_at(self, gray, tpl, step, thr, sc, cx, cy):
        """在粗找的位置附近用原尺寸再確認一次 (只比對一小塊, 很快)"""
        t = self._scaled_tpl(step["image"], tpl, round(sc, 3))
        th, tw = t.shape[:2]
        m = max(th, tw) // 2 + 8
        x0 = int(max(0, cx - tw / 2 - m))
        y0 = int(max(0, cy - th / 2 - m))
        x1 = int(min(gray.shape[1], cx + tw / 2 + m))
        y1 = int(min(gray.shape[0], cy + th / 2 + m))
        if x1 - x0 < tw or y1 - y0 < th:
            return None
        roi = gray[y0:y1, x0:x1]
        if step.get("match_mode") == "partial":
            ok, val, rx, ry = self._partial_match(roi, t, step, thr)
        else:
            ok, val, rx, ry = self._full_match(roi, t, thr)
        if not ok:
            return None
        return val, x0 + rx, y0 + ry

    def _refine_best(self, gray, tpl, step, thr, sc, cx, cy):
        lo, hi = step.get("scale_range", (1.0, 1.0))
        best = None
        for d in (0, -0.01, 0.01, -0.02, 0.02):
            v = round(sc + d, 3)
            if v < lo - 0.02 or v > hi + 0.02:
                continue
            r = self._refine_at(gray, tpl, step, thr, v, cx, cy)
            if r is None:
                continue
            if best is None or r[0] > best[0]:
                best = (r[0], r[1], r[2], v)
            if d == 0:
                break           # 同一個比例就過門檻, 不用再試其它比例
        return best

    def _hit(self, step, score, cx, cy, sc, left, top, results):
        key = step["image"]
        self.scale_cache[key] = sc
        if step.get("match_mode") != "partial":
            self.global_scale = sc      # 局部比對算出的比例只供該張圖參考, 不當成全體比例
        self.last_best = max([score] + [x[1][1] for x in results])
        return score, cx, cy, left + cx, top + cy

    def _locate(self, tpl, step, thr, fast=False, frame=None):
        """回傳 (score, client_x, client_y, screen_x, screen_y) 或 None.
        先用半尺寸畫面 + 上次成功的縮放比例快速掃描, 找到候選點再用原尺寸確認."""
        key = step["image"]
        frame = frame if frame is not None else self._grab()
        if frame is None:
            return None
        gray, left, top = frame
        scales = self._candidate_scales(key, step, fast)

        small, ds = self._downscale_for_search(gray, tpl)
        cstep, cthr = step, thr
        if ds != 1.0:
            cstep = dict(step)
            cthr = max(0.45, thr - 0.10)
            if step.get("match_mode") == "partial":
                cstep["patch_threshold"] = max(0.45, float(step.get("patch_threshold", thr)) - 0.10)
                cstep["prefilter"] = 0.25      # 縮小後的畫面分數會低一點, 門檻也放寬

        results = []
        confirmed = None
        for sc in scales:
            r = self._try(small, tpl, cstep, cthr, round(sc * ds, 3), key)
            results.append((sc, r))
            if not r[0]:
                continue
            cx, cy = r[2] / ds, r[3] / ds
            if ds == 1.0:
                cand = (r[1], cx, cy, sc)
            else:
                hit = self._refine_best(gray, tpl, step, thr, sc, cx, cy)
                if hit is None:
                    continue            # 粗找有分數但原尺寸確認沒過, 換下一個比例
                cand = hit
            if confirmed is None or cand[0] > confirmed[0]:
                confirmed = cand
            # 局部比對的分數是平均值, 要很高才算確定; 整張比對則用一般規則
            limit = 0.97 if step.get("match_mode") == "partial" else max(0.90, thr + 0.12)
            if cand[0] >= limit:
                break                   # 分數已經很高, 不用再試其它比例 (省時間)
        if confirmed is not None:
            return self._hit(step, confirmed[0], confirmed[1], confirmed[2], confirmed[3],
                             left, top, results)

        # 粗找沒有明確結果 -> 取分數最高的兩個比例, 用原尺寸再確認一次
        for sc, r in sorted(results, key=lambda x: -x[1][1])[:2]:
            if r[1] < max(0.35, thr - 0.20) or r[2] <= 0:
                continue
            hit = self._refine_best(gray, tpl, step, thr, sc, r[2] / ds, r[3] / ds)
            if hit:
                return self._hit(step, hit[0], hit[1], hit[2], hit[3], left, top, results)

        self.last_best = max([x[1][1] for x in results] or [0.0])
        return None

    # ---------- step5 / step6 偵測 ----------
    def _check_watches(self, frame, only=None):
        """檢查畫面上有沒有「鐵匠 / 死亡」圖片, 有就回傳該設定"""
        now = time.time()
        for w, ws in self._watch_steps:
            if only is not None and w is not only:
                continue
            if now < self._watch_cd.get(w["image"], 0.0):
                continue
            if self._locate(self._watch_tpl[w["image"]], ws, ws["threshold"], fast=True, frame=frame):
                return w
        return None

    def _scan_watches(self):
        if not self._watch_steps:
            return None
        frame = self._grab()
        if frame is None:
            return None
        return self._check_watches(frame)

    def _handle_watch(self, w):
        self._op_depth += 1             # 處理 step5 / step6 期間, 副手戰吼讓路
        try:
            self._handle_watch_inner(w)
        finally:
            self._op_depth -= 1

    def _handle_watch_inner(self, w):
        """偵測到特殊畫面：按設定的鍵 (空白鍵 / ESC) 離開, 並設定要跳回的步驟"""
        if self.stop_ev.is_set():
            return
        key = clean_key(self.cfg.get(w.get("key_cfg", ""), "esc"), "esc")
        n_press = min(20, max(1, int(self.cfg.get(w.get("count_cfg", ""), 1))))
        klabel = key_label(key)
        jump_to = STEP_INDEX.get(w.get("goto"), 0)
        jump_name = WORKFLOW[jump_to]["name"] if 0 <= jump_to < len(WORKFLOW) else "1. 尋找傳點"
        self.sig_status.emit(w["name"], w["image"], f"按 {klabel} ×{n_press} 離開（背景送出，回到「{jump_name}」）")
        self.sig_log.emit(f"偵測到{w['name']}，背景按 {klabel} ×{n_press} 離開後回到「{jump_name}」（不干擾其他程式）", "成功")
        self.sig_watch.emit(w["image"], f"偵測到！{time.strftime('%H:%M:%S')} 背景按 {klabel} ×{n_press}")
        log_event(f"偵測到 {w['name']} ({w['image']})")
        if w.get("count_death"):
            self.sig_death.emit()

        targets = key_targets(self.hwnd)    # [主視窗, 客戶區子視窗] 都只做後台送出
        tries = max(1, int(w.get("max_retry", 1)))
        gone = False
        for attempt in range(tries):
            if self.stop_ev.is_set():
                break
            target = targets[min(attempt, len(targets) - 1)]
            for _ in range(n_press):
                if not win32gui.IsWindow(self.hwnd):
                    break
                post_key(target, key)       # 只用 PostMessage: 不會送到其他程式, 不搶焦點
                self._sleep(0.12)
            self._sleep(float(w.get("after_wait", 1.0)))
            frame = self._grab()
            if frame is None:
                gone = True         # 截不到圖, 當作已離開, 不再重按
                break
            if self._check_watches(frame, only=w) is None:
                gone = True         # 畫面已經離開, 不用再按
                break
            if attempt + 1 >= tries:
                break
        self._watch_cd[w["image"]] = time.time() + float(w.get("cooldown", 4.0))
        self.pending_jump = jump_to
        self.sig_watch.emit(w["image"],
                            f"已離開 · {time.strftime('%H:%M:%S')} · 回到「{jump_name}」" if gone
                            else f"按 {klabel} {tries} 輪後畫面仍在 · 仍回到「{jump_name}」")

    def _apply_jump(self):
        if self.pending_jump is None:
            return None
        i = self.pending_jump
        self.pending_jump = None
        name = WORKFLOW[i]["name"] if 0 <= i < len(WORKFLOW) else "1. 尋找傳點"
        self.sig_log.emit(f"回到「{name}」重新開始", "等待中")
        return i

    def input_busy(self):
        """流程是不是正在「執行操作」: 步驟成功後的點擊 / 按鍵流程 / step5 / step6 處理 / 防卡送鍵.
        自動副手戰吼優先權最低: 這個回傳 True 的時候它一律等待.
        還在找圖、尚未成功的時候不算忙碌, 副手戰吼可以觸發; 停止或暫停中也不算忙碌 (手操時照常可用)."""
        if not self.isRunning() or not self.run_ev.is_set():
            return False
        return self._op_depth > 0 or self._acting

    # ---------- 防卡機制 ----------
    def _anti_stuck(self):
        """超過 anti_stuck_minutes 分鐘沒有成功通關 (關卡數 +1) -> 背景按設定的鍵 (空白鍵 / ESC) N 次, 然後重新計時.
        送鍵方式跟 step5 / step6 一樣: 只用 PostMessage 送給遊戲視窗, 不搶焦點、不送到其他程式.
        設定直接讀介面共用的 self.cfg, 改了馬上生效."""
        now = time.time()
        if not self.cfg.get("anti_stuck_enabled", True):
            self.anti_ref = now             # 關閉時一直重置, 之後開啟才不會立刻觸發
            return
        limit = max(0.5, float(self.cfg.get("anti_stuck_minutes", 10.0))) * 60
        idle = now - self.anti_ref
        if idle < limit:
            return
        self.anti_ref = now                 # 先重新計時: 若沒用, 再過 X 分鐘會再按一次
        self.anti_count += 1
        key = clean_key(self.cfg.get("anti_stuck_key", "space"), "space")
        n_press = min(20, max(1, int(self.cfg.get("anti_stuck_count", 1))))
        klabel = key_label(key)
        mins = int(idle // 60)
        self.sig_log.emit(f"已 {mins} 分鐘沒有成功通關，防卡觸發：背景按 {klabel} ×{n_press} (第 {self.anti_count} 次)", "失敗")
        log_event(f"防卡: {idle:.0f} 秒沒有通關, 送出 {klabel} ×{n_press} (第 {self.anti_count} 次), 目前步驟: {self.cur_step or '—'}")
        if self.monitor is not None:
            self.monitor.n.send(f"防卡{klabel}｜閒置{limit / 60:g}分鐘啟動｜已執行數{self.anti_count}", ping=True)
        self._op_depth += 1             # 防卡送鍵期間, 副手戰吼讓路
        try:
            if win32gui.IsWindow(self.hwnd):
                fake_focus(self.hwnd)       # 先送「假前景」訊息, 讓切到背景就不理按鍵的遊戲醒一下 (只是訊息, 不搶焦點)
                targets = key_targets(self.hwnd)    # 主視窗 + 客戶區子視窗都送 (防卡沒有圖可驗證, 跟 WASD / 打開包包一樣)
                for _ in range(n_press):
                    for j, target in enumerate(targets):
                        if j:
                            self._sleep(0.02)
                        post_key(target, key)
                    self._sleep(0.12)
        except Exception as e:
            log_event(f"防卡 {klabel} 失敗: {e}")
        finally:
            self._op_depth -= 1
        self._sleep(1.0)

    # ---------- 各類型步驟 ----------
    def keys_summary(self):
        seq = self.cfg.get("key_sequence", [])
        return " → ".join(seq_item_text(it) for it in seq) if seq else "(未設定按鍵)"

    def _do_keys(self, step):
        """依介面設定的順序, 用後台 PostMessage 送 Tab / Enter (不搶焦點, 不干擾其他程式)"""
        seq = [dict(x) for x in self.cfg.get("key_sequence", [])]      # 這一輪的快照
        interval = float(self.cfg.get("key_interval", 0.30))
        hold = float(self.cfg.get("key_hold", 0.08))
        if not seq:
            self.sig_log.emit("按鍵流程是空的, 略過", "等待中")
            return True
        if not self._alive():
            return False

        start = time.time()
        limit = float(step.get("max_time", MAX_KEY_TIME))
        for idx, it in enumerate(seq, 1):
            key, count = it["key"], int(it["count"])
            if key == "wait":
                self.sig_log.emit(f"{idx}. 等待 {count} 秒", "等待中")
                self._sleep(count)
                if self.stop_ev.is_set():
                    return False
                continue
            for n in range(count):
                if not self._wait_if_paused():
                    return False
                if time.time() - start > limit:
                    self.sig_log.emit(f"按鍵流程超過 {limit:g} 秒，先停下來避免卡住", "失敗")
                    return False
                post_key(self.hwnd, key, hold)
                self._beat()
                self.sig_log.emit(f"{idx}. {KEY_LABELS[key]} ({n + 1}/{count})", "成功")
                self._sleep(interval)
                if self.stop_ev.is_set():
                    return False
        self.sig_result.emit(True, "按鍵流程完成")
        if step.get("after_wait", 0) > 0:
            self.sig_log.emit(f"等待 {step['after_wait']:g} 秒", "等待中")
            self._sleep(step["after_wait"])
        return not self.stop_ev.is_set()

    def _do_step(self, step, templates, sct=None):
        """搜尋 -> 點擊 -> 等待. 成功回 True"""
        if step["image"] not in templates:                 # 圖2 變體: 第一次用到才讀取
            try:
                templates[step["image"]] = load_template(step["image"])
            except Exception as e:
                self.sig_log.emit(str(e), "失敗")
                self.sig_result.emit(False, f"讀不到圖片: {step['image']}")
                log_event(f"讀不到圖片 {step['image']}: {e}")
                return False
        tpl = templates[step["image"]]
        cands = [(step, tpl)]                               # 比對順序: 主圖 -> 備用圖
        for alt in step.get("alt_images", ()):
            if alt not in templates:
                try:
                    templates[alt] = load_template(alt)
                except Exception as e:
                    templates[alt] = None                   # 沒這個檔就記一次, 之後不再重試
                    self.sig_log.emit(f"備用圖 {alt} 讀不到，略過", "失敗")
                    log_event(f"備用圖 {alt} 讀不到: {e}")
            if templates.get(alt) is not None:
                alt_step = dict(step)
                alt_step["image"] = alt                     # 縮放比例快取以圖名區分
                cands.append((alt_step, templates[alt]))
        timeout_sec = float(step.get("timeout", DEFAULT_SEARCH_TIMEOUT))
        max_sec = float(step.get("max_time", MAX_STEP_TIME))
        start = time.time()
        deadline = start + timeout_sec
        while time.time() < deadline and not self.stop_ev.is_set():
            if not self._wait_if_paused():
                return False
            if not self._alive():
                return False
            if time.time() - start > max_sec:
                self.sig_log.emit(f"{step['name']} 卡住超過 {max_sec:g} 秒，強制跳過", "失敗")
                log_event(f"步驟卡住: {step['name']}")
                return False
            frame = self._grab(sct)                 # 同一張截圖同時給「特殊畫面」和目標比對, 省時間
            if frame is None:
                self._sleep(0.2)
                continue
            w = self._check_watches(frame)
            if w:
                self._handle_watch(w)
                return False
            r, used, best = None, step, 0.0
            for st, t in cands:
                r = self._locate(t, st, st["threshold"], frame=frame)
                best = max(best, self.last_best)
                if r is not None:
                    used = st
                    break
            self.last_best = best
            if r is None:
                self._sleep(LOOP_DELAY)
                continue
            self._acting = True             # 步驟成功 (找到圖), 接下來點擊: 副手戰吼讓路

            ox, oy = step["offset"]
            client_x, client_y = r[1] + ox, r[2] + oy
            screen_x, screen_y = r[3] + ox, r[4] + oy
            alt_note = f" (備用圖 {used['image']})" if used is not step else ""
            if step.get("action") == "key":
                # 搜尋到圖後不點擊, 直接背景送鍵 (只用 PostMessage, 不搶焦點、不送到其他程式)
                key = clean_key(step.get("key", "space"), "space")
                n_press = min(20, max(1, int(step.get("key_count", 1))))
                for _ in range(n_press):
                    if not win32gui.IsWindow(self.hwnd):
                        break
                    post_key(self.hwnd, key)
                    self._sleep(0.12)
                self.sig_log.emit(f"{step['name']}{alt_note}：背景按 {key_label(key)} ×{n_press}", "成功")
                self.sig_result.emit(True, f"成功按鍵：{step['name']} ({key_label(key)} ×{n_press})")
            else:
                click_window(self.hwnd, client_x, client_y, screen_x, screen_y, background=self.background_mode)
                self.sig_log.emit(f"點擊{step['name']}{alt_note}{'(後台不干擾)' if self.background_mode else ''}", "成功")
                self.sig_result.emit(True, f"成功點擊：{step['name']}")
            if alt_note:
                log_event(f"{step['name']}: 主圖沒找到, 改用 {used['image']} 命中")
            wait = float(self.cfg.get("delay_after_img2", 2.0)) if step.get("key_trigger") else step.get("after_wait", 0)
            if wait > 0:
                self.sig_log.emit(f"等待 {wait:g} 秒", "等待中")
                self._sleep(wait)
            return True

        if not self.stop_ev.is_set():
            self.sig_log.emit(
                f"{timeout_sec:g}秒未找到{step['name']} (最高相似度 {self.last_best:.2f}/門檻 {step['threshold']})",
                "失敗")
            self.sig_result.emit(False, f"{timeout_sec:g}秒未找到{step['name']}，跳過該步驟")
        return False

    def _resolve_step(self, step):
        """圖2: 依介面目前選的 A1~A5 決定要搜尋哪一張 (切換後下一輪就生效)"""
        vi = step.get("variant_image")
        if not vi:
            return step
        s = dict(step)
        s["image"] = vi.format(v=self.cfg.get("img2_variant", "A1"))
        return s

    def _load_templates(self):
        templates = {}
        try:
            for s in WORKFLOW:
                if s.get("type") == "keys" or s.get("variant_image"):
                    continue            # 按鍵步驟不用圖; 圖2 依介面選擇, 用到時才讀取
                templates[s["image"]] = load_template(s["image"])
                if s.get("verify_image"):
                    templates[s["verify_image"]] = load_template(s["verify_image"])
        except Exception as e:
            self.sig_log.emit(str(e), "失敗")
            self.sig_result.emit(False, "圖片載入失敗")
            log_event(f"圖片載入失敗: {e}")
            return None

        self._watch_steps = []
        for w in WATCHES:               # step5 / step6 圖片 (沒有檔案就只跳過該偵測)
            try:
                self._watch_tpl[w["image"]] = load_template(w["image"])
                self._watch_steps.append((w, {
                    "image": w["image"],
                    "threshold": w.get("threshold", 0.75),
                    "scale_range": w.get("scale_range", (0.6, 1.4)),
                }))
                self.sig_watch.emit(w["image"], f"監看中 · 門檻 {w.get('threshold', 0.75):.2f}")
            except Exception as e:
                self.sig_watch.emit(w["image"], "✖ 找不到圖片")
                self.sig_log.emit(f"略過{w['name']}偵測：{e}", "失敗")
                log_event(f"略過 {w['image']} 偵測: {e}")
        return templates

    # ---------- 主流程 ----------
    def run(self):
        try:
            log_event("流程開始")
            templates = self._load_templates()
            if templates is None:
                return
            self.grabber = WindowGrabber(self.hwnd)
            try:
                _cl = client_region(self.hwnd)
                log_event(f"遊戲視窗: 畫面區 {_cl[2]}x{_cl[3]} 位置({_cl[0]},{_cl[1]}) 視窗外框 {win32gui.GetWindowRect(self.hwnd)}")
            except Exception:
                pass
            with mss.mss() as sct:
                self.sct = sct
                i, group_fails = 0, 0
                self.anti_ref = time.time()
                while not self.stop_ev.is_set():
                    self._beat()
                    if i >= len(WORKFLOW):
                        i, group_fails = 0, 0        # 一輪做完, 重頭循環
                        continue

                    j = self._apply_jump()
                    if j is not None:
                        i, group_fails = j, 0
                        continue

                    step = self._resolve_step(WORKFLOW[i])
                    if not self._wait_if_paused():
                        return
                    if not self._alive():
                        return

                    # 每個步驟開始前, 先看畫面上有沒有「鐵匠 / 死亡」畫面
                    w = self._scan_watches()
                    if w:
                        if self.monitor is not None:
                            self.monitor.progress({"ends_battle": True})   # 死亡 / 鐵匠畫面 = 已不在戰鬥
                        self._handle_watch(w)
                        j = self._apply_jump()
                        if j is not None:
                            i, group_fails = j, 0
                        continue

                    self._anti_stuck()          # 太久沒通關 -> 按 ESC

                    self.cur_step = step["name"]
                    self._acting = False            # 每個步驟從「找圖」開始, 還沒成功不算操作中
                    self.cur_thr = step.get("threshold")
                    if step.get("type") == "keys" and self.monitor is not None:
                        self.monitor.touch()
                    is_keys = step.get("type") == "keys"
                    self.sig_status.emit(step["name"], step.get("image", "—"),
                                         self.keys_summary() if is_keys else step["next"])
                    self.sig_log.emit(step["name"] if is_keys else f"搜尋 {step['name']}", "等待中")
                    self._acting = is_keys          # 按鍵流程一開始就是操作; 圖片步驟要找到圖才算 (見 _do_step)
                    ok = self._do_keys(step) if is_keys else self._do_step(step, templates, sct)
                    self._acting = False            # 這個步驟做完了 (成功或失敗), 副手戰吼可以再觸發

                    j = self._apply_jump()          # 搜尋途中偵測到 step5 / step6
                    if j is not None:
                        i, group_fails = j, 0
                        continue
                    if self.stop_ev.is_set():
                        return

                    if ok:
                        if self.monitor is not None:
                            self.monitor.progress(step)
                        if step.get("count_round"):
                            self.anti_ref = time.time()     # 成功通關 -> 防卡重新計時
                            self.sig_round.emit()
                        i += 1
                        continue

                    # ---- 失敗處理 ----
                    if self.skip_on_fail or step.get("on_fail") == "next":
                        grp = step.get("group")
                        if grp:
                            # 與該步驟同 group 的後續操作（例如 TAB 鍵盤操作）綁在一起：
                            # 第一步圖片未搜到跳過時，同組綁定的 TAB 鍵盤操作一併略過！
                            self.sig_log.emit(f"{step['name']} 未完成，同組綁定步驟 (含按鍵流程) 一併略過", "失敗")
                            while i < len(WORKFLOW) and WORKFLOW[i].get("group") == grp:
                                i += 1
                        else:
                            self.sig_log.emit(f"{step['name']} 未搜到，跳至下一步 (循環執行)", "失敗")
                            i += 1

                        self._sleep(FAIL_STEP_DELAY)
                        if i >= len(WORKFLOW):
                            i = 0                     # 到達最後一步，重頭循環 (不斷重複)
                        continue

                    self._sleep(1.0)
                    grp = step.get("group")
                    if grp and group_fails < GROUP_RETRIES:
                        group_fails += 1
                        while i > 0 and WORKFLOW[i - 1].get("group") == grp:
                            i -= 1                    # 退回該組的第一步
                        self.sig_log.emit(f"流程 {grp} 整組重做 ({group_fails}/{GROUP_RETRIES})", "失敗")
                    else:
                        i, group_fails = 0, 0         # 沒有分組或重做次數用完 -> 從第一步重來
        except Exception as e:
            log_event("流程發生錯誤\n" + traceback.format_exc())
            self.sig_log.emit(f"流程錯誤: {e}", "失敗")
            self.sig_result.emit(False, f"流程錯誤: {e}")
        finally:
            if self.grabber is not None:
                self.grabber.close()
                self.grabber = None
            self.sct = None
            log_event("流程結束")


QSS = """
QWidget { background:#000; color:#ddd; font-family:'Microsoft JhengHei UI'; font-size:13px; }
QLabel#h { font-size:15px; font-weight:bold; color:#fff; }
QLabel#big { font-size:22px; font-weight:bold; color:#fff; }
QLabel#badge { background:#0b2a14; color:#2ecc71; border-radius:10px; padding:2px 10px; font-size:12px; }
QFrame#card { border:1px solid #222; border-radius:10px; }
QPushButton { background:#111; border:1px solid #333; border-radius:6px; padding:5px 10px; font-weight:bold; }
QPushButton:hover { background:#1c1c1c; }
QPushButton:disabled { color:#555; }
QLineEdit { background:#0d0d0d; border:1px solid #333; border-radius:6px; padding:6px; color:#fff; }
QListWidget { background:#0d0d0d; border:1px solid #333; border-radius:6px; }
QListWidget::item { padding:3px 6px; }
QListWidget::item:selected { background:#1f3a5f; color:#fff; }
QSpinBox, QDoubleSpinBox { background:#0d0d0d; border:1px solid #333; border-radius:6px; padding:4px; color:#fff; }
QTabWidget::pane { border:1px solid #222; border-radius:6px; }
QTabBar::tab { background:#111; border:1px solid #333; padding:5px 14px; border-top-left-radius:6px; border-top-right-radius:6px; }
QTabBar::tab:selected { background:#1f3a5f; color:#fff; }
QTableWidget { border:none; gridline-color:#000; }
QHeaderView::section { background:#000; border:none; }
"""


class HotkeyBridge(QObject):
    """把 keyboard 執行緒的熱鍵事件安全地轉到 Qt 主執行緒"""
    triggered = Signal(str)


class Main(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("D2Auto 控制台")
        scr = QApplication.primaryScreen().availableGeometry()
        self.resize(min(500, scr.width() - 40), min(640, int(scr.height() * 0.85)))
        self.setMinimumSize(460, 400)
        self.hwnd = None
        self.cfg = load_config()        # 按鍵流程設定 (啟動時自動讀取)
        self.worker = None
        self.elapsed = 0
        self._last_clear_elapsed = 0    # 上次通關時的「已執行秒數」, 用來算本局時間
        self.rounds = 0                 # 已刷關卡次數 (累計, 可手動歸零)
        self.deaths = 0                 # 死亡次數 (step6 偵測到就 +1, 可手動歸零)
        self.run_rounds = 0             # 本次執行完成的關卡數 (算平均時間用)
        self.state = "stopped"          # stopped / running / paused
        self._wd_reported = 0           # 卡死監看的警告狀態
        self._keep_time = False         # 卡死自動重啟時, 保留目前時間/統計

        self.notifier = DiscordNotifier()
        log_event(f"Discord 通知狀態: {self.notifier.status_text()}")
        self.freeze = FreezeMonitor(self.notifier)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)

        # WASD 移動: 記錄目前按住的鍵, 計時器定期重送「按下」訊息
        self.move_held = set()
        self._move_warned = False
        self._move_fail_warned = False
        self.move_timer = QTimer(self)
        self.move_timer.setInterval(120)
        self.move_timer.timeout.connect(self._move_tick)
        self.offhand_on = False             # 自動副手戰吼 (預設 OFF, 不跨次記憶)
        self._offhand_busy = False
        self._offhand_warned = False
        self.offhand_timer = QTimer(self)
        self.offhand_timer.timeout.connect(self._offhand_fire)
        self._key_rows = []                 # (按鍵下拉, 次數, cfg 鍵名, cfg 次數名)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ===== 常駐頂部 (永遠看得到): 控制 / 狀態 / 視窗鎖定 =====
        row = QHBoxLayout()
        self.btn_start = QPushButton("▶ 開始 (F6)")
        self.btn_pause = QPushButton("⏸ 暫停 (F7)")
        self.btn_stop = QPushButton("■ 停止 (F8)")
        self.btn_start.clicked.connect(self.start)
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_stop.clicked.connect(self.stop)
        for b in (self.btn_start, self.btn_pause, self.btn_stop):
            row.addWidget(b)
        self.btn_pause.setEnabled(False); self.btn_stop.setEnabled(False)
        row.addStretch()
        self.badge_run = self.badge("待命"); row.addWidget(self.badge_run)
        root.addLayout(row)

        row = QHBoxLayout()
        self.lbl_time = QLabel("00:00:00"); self.lbl_time.setObjectName("h"); row.addWidget(self.lbl_time)
        self.btn_reset_time = QPushButton("↺ 重設時間")
        self.btn_reset_time.setToolTip("把目前的執行時間歸零 (不影響關卡 / 死亡次數)")
        self.btn_reset_time.clicked.connect(self.reset_time)
        row.addWidget(self.btn_reset_time)
        row.addStretch()
        root.addLayout(row)

        row = QHBoxLayout()
        self.cmb_win = QComboBox()
        self.cmb_win.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_win.setMinimumContentsLength(14)
        self.btn_refresh = QPushButton("↻"); self.btn_refresh.setToolTip("重新整理視窗清單")
        self.btn_refresh.clicked.connect(self.refresh_windows)
        self.btn_lock = QPushButton("鎖定選取"); self.btn_lock.clicked.connect(self.lock_selected)
        row.addWidget(self.cmb_win, 1); row.addWidget(self.btn_refresh); row.addWidget(self.btn_lock)
        root.addLayout(row)

        row = QHBoxLayout()
        self.btn_lock_fg = QPushButton("3秒後鎖定我點的視窗"); self.btn_lock_fg.clicked.connect(self.lock_countdown)
        self.badge_lock = self.badge("未鎖定")
        self.lbl_win = QLabel("尚未鎖定")
        self.lbl_win.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.btn_lock_fg); row.addWidget(self.badge_lock); row.addWidget(self.lbl_win, 1)
        root.addLayout(row)

        # ===== WASD / 打開包包 / 呼回遊戲視窗 =====
        row = QHBoxLayout()
        row.setSpacing(6)
        move_tip = QLabel("WASD\n移動")
        move_tip.setAlignment(Qt.AlignCenter)
        move_tip.setToolTip("按住按鈕 = 持續移動，放開 = 停止\n"
                            "也可以直接按鍵盤 W / A / S / D 移動、C 打開包包\n"
                            "全部用背景送鍵，不搶焦點、不影響其他程式\n"
                            "WASD沒生效的時候按一下")
        row.addWidget(move_tip, 0, Qt.AlignVCenter)
        pad = QGridLayout()
        pad.setHorizontalSpacing(4); pad.setVerticalSpacing(4)
        self.move_buttons = {}
        for key, label, r, c in (("w", "W ↑ 上", 0, 1), ("a", "A ← 左", 1, 0),
                                 ("s", "S ↓ 下", 1, 1), ("d", "D → 右", 1, 2)):
            b = QPushButton(label)
            b.setMinimumSize(52, 28)
            b.setAutoRepeat(False)
            b.setToolTip("按住 = 持續移動，放開 = 停止（背景送鍵，不搶焦點）")
            b.pressed.connect(lambda k=key: self.move_press(k))
            b.released.connect(lambda k=key: self.move_release(k))
            self.move_buttons[key] = b
            pad.addWidget(b, r, c)
        row.addLayout(pad)

        side = QGridLayout()
        side.setHorizontalSpacing(4); side.setVerticalSpacing(4)
        self.btn_bag = QPushButton("打開包包 (C)")
        self.btn_bag.setMinimumHeight(28)
        self.btn_bag.setToolTip("對遊戲送出 C（也可以直接按鍵盤 C）")
        self.btn_bag.clicked.connect(self.bag_press)
        self.btn_front = QPushButton("顯現遊戲視窗在最前")
        self.btn_front.setMinimumHeight(28)
        self.btn_front.setStyleSheet("font-size:11px;")
        self.btn_front.setToolTip("把鎖定的遊戲視窗還原並移到最前面\nWASD沒生效的時候按一下")
        self.btn_front.clicked.connect(self.bring_game_front)
        self.btn_offhand = QPushButton("自動副手戰吼 OFF")
        self.btn_offhand.setMinimumHeight(28)
        self.btn_offhand.clicked.connect(self.offhand_toggle)
        self.btn_offhand_cfg = QPushButton("切副手設定")
        self.btn_offhand_cfg.setMinimumHeight(28)
        self.btn_offhand_cfg.setStyleSheet("font-size:11px; padding:5px 6px;")
        self.btn_offhand_cfg.setToolTip("設定：每隔幾秒、快速按幾次、哪一個按鍵")
        self.btn_offhand_cfg.clicked.connect(self.offhand_settings)
        self._offhand_refresh()
        side.addWidget(self.btn_bag, 0, 0)
        side.addWidget(self.btn_front, 1, 0)
        side.addWidget(self.btn_offhand, 0, 1)
        side.addWidget(self.btn_offhand_cfg, 1, 1)
        row.addLayout(side)
        row.addStretch()
        root.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("第二張圖"))
        self.cmb_img2 = QComboBox()
        for name in IMG2_VARIANTS:
            self.cmb_img2.addItem(name, name)
        self.cmb_img2.setCurrentIndex(max(0, self.cmb_img2.findData(self.cfg["img2_variant"])))
        self.cmb_img2.currentIndexChanged.connect(self.img2_changed)
        self.lbl_img2 = QLabel()
        self.lbl_img2.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.cmb_img2); row.addWidget(self.lbl_img2, 1)
        root.addLayout(row)

        # ===== 分頁 =====
        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        def scroll_page(inner):
            sa = QScrollArea()
            sa.setWidgetResizable(True)
            sa.setFrameShape(QFrame.NoFrame)
            sa.setWidget(inner)
            return sa

        # ---- 分頁 1: 狀態 / 紀錄 ----
        p1 = QWidget()
        v = QVBoxLayout(p1); v.setContentsMargins(6, 6, 6, 6); v.setSpacing(4)
        self.lbl_search = QLabel("目前任務"); v.addWidget(self.lbl_search)
        self.lbl_target = QLabel("-"); self.lbl_target.setObjectName("h"); v.addWidget(self.lbl_target)
        self.lbl_img = QLabel("比對圖片：-"); v.addWidget(self.lbl_img)
        v.addWidget(QLabel("每局計時 / 防卡時間 (通關後重置)"))
        self.lbl_round = QLabel(""); self.lbl_round.setObjectName("h"); v.addWidget(self.lbl_round)
        v.addWidget(QLabel("特殊畫面監看 (step5 / step6)"))
        self.lbl_watch = {}
        for w in WATCHES:
            lab = QLabel(f"{w['name']}：待命")
            lab.setWordWrap(True)
            lab.setStyleSheet("color:#888;")
            lab.setToolTip("偵測到時只用背景 PostMessage 送按鍵 (空白鍵 / ESC，在「統計 / 設定」設定) 給遊戲視窗，"
                           "不會送到其他程式、不搶焦點")
            self.lbl_watch[w["image"]] = lab
            v.addWidget(lab)
        tip = QLabel("step5 / step6 / 防卡 的按鍵一律背景送出，不搶焦點、不干擾其他程式")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#777;")
        v.addWidget(tip)
        v.addWidget(QLabel("下一步操作"))
        self.lbl_next = QLabel("-"); self.lbl_next.setObjectName("h"); self.lbl_next.setWordWrap(True)
        v.addWidget(self.lbl_next)
        self.lbl_result = QLabel("—"); self.lbl_result.setWordWrap(True); v.addWidget(self.lbl_result)
        v.addWidget(self.h("操作紀錄"))
        self.table = QTableWidget(0, 3)
        self.table.horizontalHeader().setVisible(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setMinimumHeight(90)
        row_h = self.table.verticalHeader().defaultSectionSize()
        self.table.setFixedHeight(row_h * 3 + 4)   # 約 3 行高, 仍可捲動看歷史紀錄
        v.addWidget(self.table)
        tabs.addTab(scroll_page(p1), "狀態")

        # ---- 分頁 2: 按鍵流程 (圖片2 點擊成功後執行, 自動儲存) ----
        p2 = QWidget()
        v = QVBoxLayout(p2); v.setContentsMargins(6, 6, 6, 6); v.setSpacing(6)
        hint = QLabel("圖片2 點擊成功後執行 · 後台送鍵, 不搶焦點不影響其他程式\n修改會自動儲存, 下一輪生效")
        hint.setWordWrap(True); hint.setStyleSheet("color:#888;")
        v.addWidget(hint)
        self.lst_seq = QListWidget(); self.lst_seq.setMinimumHeight(90)
        self.lst_seq.currentRowChanged.connect(self.seq_pick)
        v.addWidget(self.lst_seq, 1)

        row = QHBoxLayout()
        self.cmb_key = QComboBox()
        for k, label in KEY_LABELS.items():
            self.cmb_key.addItem(label, k)
        self.spn_count = QSpinBox(); self.spn_count.setRange(1, 99); self.spn_count.setValue(1)
        self.cmb_key.currentIndexChanged.connect(self.seq_sync_suffix)
        self.seq_sync_suffix()
        b_add = QPushButton("新增"); b_add.clicked.connect(self.seq_add)
        b_upd = QPushButton("修改選取"); b_upd.clicked.connect(self.seq_update)
        row.addWidget(self.cmb_key, 1); row.addWidget(self.spn_count); row.addWidget(b_add); row.addWidget(b_upd)
        v.addLayout(row)

        row = QHBoxLayout()
        b_up = QPushButton("↑ 上移"); b_up.clicked.connect(lambda: self.seq_move(-1))
        b_dn = QPushButton("↓ 下移"); b_dn.clicked.connect(lambda: self.seq_move(1))
        b_del = QPushButton("刪除"); b_del.clicked.connect(self.seq_delete)
        for b in (b_up, b_dn, b_del):
            row.addWidget(b)
        v.addLayout(row)

        form = QFormLayout()
        self.spn_interval = QDoubleSpinBox(); self.spn_interval.setRange(0.05, 3.0); self.spn_interval.setSingleStep(0.05)
        self.spn_interval.setDecimals(2); self.spn_interval.setValue(self.cfg["key_interval"])
        self.spn_hold = QDoubleSpinBox(); self.spn_hold.setRange(0.02, 1.0); self.spn_hold.setSingleStep(0.02)
        self.spn_hold.setDecimals(2); self.spn_hold.setValue(self.cfg["key_hold"])
        self.spn_delay = QDoubleSpinBox(); self.spn_delay.setRange(0.0, 30.0); self.spn_delay.setSingleStep(0.5)
        self.spn_delay.setDecimals(1); self.spn_delay.setValue(self.cfg["delay_after_img2"])
        for lbl, w in (("按鍵間隔", self.spn_interval), ("按住時間", self.spn_hold), ("圖2點擊後等待", self.spn_delay)):
            w.setSuffix(" 秒")
            w.valueChanged.connect(self.cfg_changed)
            form.addRow(lbl, w)
        v.addLayout(form)
        tabs.addTab(scroll_page(p2), "按鍵流程")

        # ---- 分頁 3: 統計 / 設定 ----
        p3 = QWidget()
        v = QVBoxLayout(p3); v.setContentsMargins(6, 6, 6, 6); v.setSpacing(6)
        g = QGridLayout(); g.setHorizontalSpacing(10); g.setVerticalSpacing(3)
        self.in_start = QLineEdit("0")
        self.in_end = QLineEdit("0")
        self.btn_calc = QPushButton("計算收益"); self.btn_calc.clicked.connect(self.update_income)
        self.in_start.textChanged.connect(self.update_income)
        self.in_end.textChanged.connect(self.update_income)
        g.addWidget(QLabel("現在金額"), 0, 0); g.addWidget(QLabel("結束金額"), 0, 1)
        g.addWidget(self.in_start, 1, 0); g.addWidget(self.in_end, 1, 1); g.addWidget(self.btn_calc, 1, 2)
        self.lbl_total = QLabel("+0"); self.lbl_total.setObjectName("h")
        self.lbl_per_round = QLabel("--"); self.lbl_per_round.setObjectName("h")
        self.lbl_pm = QLabel("0.00"); self.lbl_pm.setObjectName("h")
        g.addWidget(QLabel("總收益"), 2, 0); g.addWidget(QLabel("每場收益"), 2, 1); g.addWidget(QLabel("每分鐘收益"), 2, 2)
        g.addWidget(self.lbl_total, 3, 0); g.addWidget(self.lbl_per_round, 3, 1); g.addWidget(self.lbl_pm, 3, 2)
        self.lbl_rounds = QLabel("0"); self.lbl_rounds.setObjectName("h")
        self.lbl_deaths = QLabel("0"); self.lbl_deaths.setObjectName("h")
        self.lbl_avg = QLabel("--"); self.lbl_avg.setObjectName("h")
        self.btn_reset = QPushButton("歸零次數"); self.btn_reset.clicked.connect(self.reset_rounds)
        self.btn_reset_death = QPushButton("歸零死亡"); self.btn_reset_death.clicked.connect(self.reset_deaths)
        g.addWidget(QLabel("已刷關卡次數"), 4, 0); g.addWidget(QLabel("死亡次數"), 4, 1)
        g.addWidget(QLabel("平均每關時間"), 4, 2)
        g.addWidget(self.lbl_rounds, 5, 0); g.addWidget(self.lbl_deaths, 5, 1)
        g.addWidget(self.lbl_avg, 5, 2)
        g.addWidget(self.btn_reset, 6, 0); g.addWidget(self.btn_reset_death, 6, 1)
        v.addLayout(g)

        form = QFormLayout()
        self.cmb_fail_mode = QComboBox()
        self.cmb_fail_mode.addItem("失敗直接跳下一步 (不斷重複)", True)
        self.cmb_fail_mode.addItem("整組重試 / 從頭開始", False)
        self.cmb_op_mode = QComboBox()
        self.cmb_op_mode.addItem("後台靜默 (完全不干擾)", True)
        self.cmb_op_mode.addItem("前台 (會動滑鼠/搶焦點)", False)
        form.addRow("失敗策略", self.cmb_fail_mode)
        form.addRow("操作模式", self.cmb_op_mode)

        self.chk_anti = QCheckBox("啟用防卡")
        self.chk_anti.setChecked(bool(self.cfg["anti_stuck_enabled"]))
        self.spn_anti = QDoubleSpinBox(); self.spn_anti.setRange(0.5, 120.0); self.spn_anti.setSingleStep(0.5)
        self.spn_anti.setDecimals(1); self.spn_anti.setSuffix(" 分鐘")
        self.spn_anti.setValue(float(self.cfg["anti_stuck_minutes"]))
        self.spn_anti.setEnabled(self.chk_anti.isChecked())
        self.spn_anti.setToolTip("超過這麼久沒有成功通關 (關卡數 +1) 就背景按一次防卡鍵。\n請設得比正常一局的時間長, 否則戰鬥中會被誤按。")
        self.chk_anti.toggled.connect(self.spn_anti.setEnabled)
        self.chk_anti.toggled.connect(self.cfg_changed)
        self.spn_anti.valueChanged.connect(self.cfg_changed)
        self.spn_idle = QDoubleSpinBox(); self.spn_idle.setRange(0.0, 720.0); self.spn_idle.setSingleStep(0.5)
        self.spn_idle.setDecimals(1); self.spn_idle.setSuffix(" 分鐘"); self.spn_idle.setSpecialValueText("關閉")
        self.spn_idle.setValue(float(self.cfg["idle_report_minutes"]))
        self.spn_idle.setToolTip("超過這麼久沒有成功通關 (關卡數 +1) 就 DC 通知並 TAG 你。0 = 關閉。\n與上面的防卡 ESC 各自獨立計時。")
        self.spn_idle.valueChanged.connect(self.cfg_changed)
        c_anti, n_anti, box_anti = self._key_count_row("anti_stuck_key", "anti_stuck_count")
        c_anti.setEnabled(self.chk_anti.isChecked()); n_anti.setEnabled(self.chk_anti.isChecked())
        self.chk_anti.toggled.connect(c_anti.setEnabled); self.chk_anti.toggled.connect(n_anti.setEnabled)
        c_s5, n_s5, box_s5 = self._key_count_row("step5_key", "step5_count")
        c_s6, n_s6, box_s6 = self._key_count_row("step6_key", "step6_count")
        form.addRow(self.chk_anti)
        form.addRow("幾分鐘沒通關就防卡", self.spn_anti)
        form.addRow("防卡按鍵 / 次數", box_anti)
        form.addRow("閒置沒通關DC回報", self.spn_idle)
        form.addRow("step5 鐵匠 技能 包包防卡", box_s5)
        form.addRow("step6 死亡畫面", box_s6)
        v.addLayout(form)

        row = QHBoxLayout()
        btn_log = QPushButton("開啟執行紀錄檔")
        btn_log.setToolTip(f"程式執行紀錄 / 錯誤原因：{LOG_PATH}")
        btn_log.clicked.connect(self.open_log)
        row.addWidget(btn_log); row.addStretch()
        v.addLayout(row)
        v.addStretch()
        tabs.addTab(scroll_page(p3), "統計 / 設定")

        self.setStyleSheet(QSS)
        self.update_income()
        self.refresh_windows()
        self.seq_refresh(save=False)
        self.img2_label()
        self.setup_hotkeys()
        QApplication.instance().installEventFilter(self)   # 接收 W/A/S/D 移動鍵

    # ---------- UI 小工具 ----------
    def h(self, text):
        l = QLabel(text); l.setObjectName("h"); return l

    def badge(self, text):
        l = QLabel(text); l.setObjectName("badge"); return l

    def add_log(self, text, status):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(time.strftime("%H:%M:%S")))
        mid = QTableWidgetItem(text); mid.setTextAlignment(Qt.AlignCenter)
        st = QTableWidgetItem(status); st.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        st.setForeground(Qt.green if status == "成功" else Qt.red if status == "失敗" else Qt.gray)
        self.table.setItem(r, 0, mid); self.table.setItem(r, 1, st)
        if r > 200:
            self.table.removeRow(0)
        self.table.scrollToBottom()

    # ---------- WASD 移動 (後台 PostMessage, 不搶焦點) ----------
    MOVE_KEYMAP = {"w", "a", "s", "d"}

    def _move_target(self):
        if self.hwnd and win32gui.IsWindow(self.hwnd):
            return self.hwnd
        return None

    def _move_send(self, key, down, repeat=False):
        hwnd = self._move_target()
        if hwnd is None:
            return False
        try:
            if down and not repeat:
                fake_focus(hwnd)        # 先送「假前景」訊息, 讓切到背景就不理按鍵的遊戲醒一下 (只是訊息, 不搶焦點)
            targets = key_targets(hwnd)
            if down and not repeat:
                log_event(f"WASD 按下 {key.upper()} -> hwnd={hwnd} 目標視窗 {targets}")
            for target in targets:
                if down:
                    post_key_down(target, key, repeat)
                else:
                    post_key_up(target, key)
        except Exception as e:
            # 常見原因: 遊戲以系統管理員身分執行但本程式不是 (UIPI 擋訊息), 或遊戲重開後視窗代碼已失效
            log_event(f"WASD 送鍵失敗 (按鍵 {key}, hwnd={hwnd}): {e}")
            if not self._move_fail_warned:
                self._move_fail_warned = True       # 介面只提示一次, 其餘寫進紀錄檔, 避免洗版
                self.add_log(f"WASD 送鍵失敗: {e} (詳見 d2auto_log.txt)", "失敗")
            return False
        self._move_fail_warned = False
        return True

    def move_press(self, key):
        if key not in self.MOVE_KEYMAP:
            return
        if self._move_target() is None:
            if not self._move_warned:
                self._move_warned = True
                self.add_log("尚未鎖定遊戲視窗，WASD 無法送出", "失敗")
            return
        self._move_warned = False
        if key in self.move_held:
            return
        self.move_held.add(key)
        self._move_send(key, True)
        if not self.move_timer.isActive():
            self.move_timer.start()

    def move_release(self, key):
        if key not in self.move_held:
            return
        self.move_held.discard(key)
        self._move_send(key, False)
        if not self.move_held:
            self.move_timer.stop()

    def move_release_all(self):
        for key in list(self.move_held):
            self.move_release(key)

    def _move_tick(self):
        """按住不放時定期重送按鍵, 避免遊戲漏掉 (放開時一定送 keyup)"""
        if not self.move_held:
            self.move_timer.stop()
            return
        if self._move_target() is None:
            self.move_release_all()
            return
        for key in list(self.move_held):
            self._move_send(key, True, repeat=True)   # 模擬按住的重複訊息

    @staticmethod
    def _is_text_widget(w):
        while w is not None:
            if isinstance(w, (QLineEdit, QSpinBox, QDoubleSpinBox)):
                return True
            w = w.parentWidget() if hasattr(w, "parentWidget") else None
        return False

    def eventFilter(self, obj, ev):
        """在這個視窗按 W/A/S/D 就送出移動 (輸入框打字時不攔截)"""
        t = ev.type()
        if t in (QEvent.KeyPress, QEvent.KeyRelease):
            try:
                win = obj.window()
            except AttributeError:
                win = None
            if win is self and not self._is_text_widget(obj) and not ev.isAutoRepeat():
                key = {Qt.Key_W: "w", Qt.Key_A: "a", Qt.Key_S: "s", Qt.Key_D: "d"}.get(ev.key())
                if key:
                    if t == QEvent.KeyPress:
                        self.move_press(key)
                    else:
                        self.move_release(key)
                    return True
                if ev.key() == Qt.Key_C and ev.modifiers() == Qt.NoModifier:
                    if t == QEvent.KeyPress:
                        self.bag_press()
                    return True
        elif t == QEvent.WindowDeactivate and obj is self:
            self.move_release_all()       # 視窗被切走時, 避免按鍵卡住
        return super().eventFilter(obj, ev)

    # ---------- 包包鍵 (C) / 呼回遊戲視窗 ----------
    def bag_press(self):
        """背景送出 C 打開包包 (和 WASD 一樣不搶焦點)"""
        hwnd = self._move_target()
        if hwnd is None:
            if not self._move_warned:
                self._move_warned = True
                self.add_log("尚未鎖定遊戲視窗，無法送出按鍵", "失敗")
            return
        self._move_warned = False
        try:
            log_event(f"打開包包: 送出 C 鍵 (hwnd={hwnd})")
            for i, target in enumerate(key_targets(hwnd)):
                if i:
                    time.sleep(0.03)
                post_key(target, "c", hold=0.05)
        except Exception as e:
            log_event(f"送出包包鍵失敗: {e}")
            self.add_log(f"送出包包鍵失敗: {e}", "失敗")

    # ---------- 設定列: 按鍵 (空白鍵 / ESC) + 次數 ----------
    def _key_count_row(self, key_name, count_name):
        cmb = QComboBox()
        for k, label in KEY_CHOICES:
            cmb.addItem(label, k)
        cmb.setCurrentIndex(max(0, cmb.findData(self.cfg[key_name])))
        spn = QSpinBox(); spn.setRange(1, 20); spn.setSuffix(" 次"); spn.setValue(int(self.cfg[count_name]))
        cmb.currentIndexChanged.connect(self.cfg_changed)
        spn.valueChanged.connect(self.cfg_changed)
        box = QHBoxLayout(); box.addWidget(cmb); box.addWidget(spn); box.addStretch()
        self._key_rows.append((cmb, spn, key_name, count_name))
        return cmb, spn, box

    # ---------- 自動副手戰吼 (手操衝層用) ----------
    OFFHAND_TIP = "預計手操衝層使用的，掛機自行斟酌\n久了誤操會很慘"
    OFFHAND_FREE_SEC = 0.3          # 流程要連續閒置這麼久才送 (避開步驟與步驟之間的空檔)
    OFFHAND_MAX_WAIT = 180          # 流程一直忙, 最多等幾秒; 超過就略過這一輪

    def _worker_busy(self):
        """自動流程是不是正在執行操作 (步驟成功後的點擊 / 按鍵流程 / step5 / step6 / 防卡); 找圖搜尋中不算"""
        w = self.worker
        try:
            return w is not None and w.input_busy()
        except Exception:
            return False

    def _offhand_refresh(self):
        on = self.offhand_on
        self.btn_offhand.setText(f"自動副手戰吼 {'ON' if on else 'OFF'}")
        self.btn_offhand.setStyleSheet(
            "font-size:11px; padding:5px 6px; "
            + ("background:#1e4d2b; border:1px solid #2ecc71; color:#2ecc71;" if on else "color:#aaa;"))
        self.btn_offhand.setToolTip(self.OFFHAND_TIP)

    def offhand_settings(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("切副手設定")
        form = QFormLayout(dlg)
        spn_sec = QDoubleSpinBox(); spn_sec.setRange(0.5, 3600.0); spn_sec.setDecimals(1)
        spn_sec.setSingleStep(0.5); spn_sec.setSuffix(" 秒")
        spn_sec.setValue(float(self.cfg["offhand_interval"]))
        spn_cnt = QSpinBox(); spn_cnt.setRange(1, 20); spn_cnt.setSuffix(" 次")
        spn_cnt.setValue(int(self.cfg["offhand_count"]))
        ed = QComboBox(); ed.setEditable(True)          # 可下拉選常用鍵, 也可自己輸入
        for k in ("", "tab", "enter", "esc", "space", "up", "down", "left", "right"):
            ed.addItem(k)
        ed.setEditText(str(self.cfg.get("offhand_key", "")))
        ed.lineEdit().setPlaceholderText("選擇或輸入，例如 tab / enter / esc / space / x")
        err = QLabel(""); err.setStyleSheet("color:#ff6b6b;"); err.setWordWrap(True)
        note = QLabel(self.OFFHAND_TIP); note.setStyleSheet("color:#888;")
        form.addRow("重複間隔 (每幾秒)", spn_sec)
        form.addRow("每次快速按幾次", spn_cnt)
        form.addRow("按鍵", ed)
        form.addRow(err)
        form.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        form.addRow(bb)

        def ok():
            k = normalize_key(ed.currentText())
            if not k:
                err.setText("請輸入要按的按鍵 (沒有預設)")
                return
            try:
                _vk(k)
            except ValueError:
                err.setText(f"不支援的按鍵: {k}（可用單一字元、tab、enter、esc、space、方向鍵、f1~f12）")
                return
            self.cfg["offhand_key"] = k
            self.cfg["offhand_count"] = int(spn_cnt.value())
            self.cfg["offhand_interval"] = round(spn_sec.value(), 1)
            save_config(self.cfg)
            dlg.accept()

        bb.accepted.connect(ok)
        bb.rejected.connect(dlg.reject)
        if dlg.exec() == QDialog.Accepted:
            if self.offhand_on:
                self.offhand_timer.setInterval(int(float(self.cfg["offhand_interval"]) * 1000))
            self._offhand_refresh()

    def offhand_toggle(self):
        if not self.offhand_on:
            if not str(self.cfg.get("offhand_key", "")).strip():
                self.add_log("自動副手戰吼：尚未設定按鍵，請先設定", "失敗")
                self.offhand_settings()
                if not str(self.cfg.get("offhand_key", "")).strip():
                    return
            self.offhand_on = True
            self.offhand_timer.setInterval(int(float(self.cfg["offhand_interval"]) * 1000))
            self.offhand_timer.start()
            self.add_log(f"自動副手戰吼 ON：每 {float(self.cfg['offhand_interval']):g} 秒按 "
                         f"{key_label(self.cfg['offhand_key'])} × {int(self.cfg['offhand_count'])}", "成功")
            log_event("自動副手戰吼 ON")
            self._offhand_fire()            # 剛開啟先按一輪 (流程忙的話會等它做完)
        else:
            self.offhand_on = False
            self.offhand_timer.stop()
            self.add_log("自動副手戰吼 OFF", "等待中")
            log_event("自動副手戰吼 OFF")
        self._offhand_refresh()

    def _offhand_fire(self):
        """送一輪副手戰吼. 優先權最低: 自動流程正在「執行操作」(步驟成功後的點擊 / 按鍵流程 / step5 / step6 / 防卡) 時先等,
        等它連續閒置一小段時間才送; 還在找圖的時候照常送. 送的途中流程又開始操作也會停下來等 (在另一個執行緒等, 不會卡住介面)."""
        if not self.offhand_on or self._offhand_busy:
            return
        hwnd = self._move_target()
        if hwnd is None:
            if not self._offhand_warned:
                self._offhand_warned = True
                self.add_log("尚未鎖定遊戲視窗，自動副手戰吼無法送出", "失敗")
            return
        self._offhand_warned = False
        key = normalize_key(self.cfg.get("offhand_key", ""))
        cnt = min(20, max(1, int(self.cfg.get("offhand_count", 2))))
        if not key:
            return
        self._offhand_busy = True       # 上一輪還在等 / 送的時候, 下一輪不疊上去

        def wait_free():
            """等到流程連續閒置 OFFHAND_FREE_SEC 秒; 被關掉或等太久回傳 False"""
            waited = free = 0.0
            while self.offhand_on:
                if self._worker_busy():
                    free = 0.0
                else:
                    free += 0.05
                    if free >= self.OFFHAND_FREE_SEC:
                        return True
                waited += 0.05
                if waited > self.OFFHAND_MAX_WAIT:
                    log_event("自動副手戰吼: 流程一直忙碌, 略過這一輪")
                    return False
                time.sleep(0.05)
            return False

        def run():
            try:
                for _ in range(cnt):
                    if not wait_free():
                        return
                    if not win32gui.IsWindow(hwnd):
                        return
                    for j, target in enumerate(key_targets(hwnd)):   # 跟 WASD / 打開包包一樣, 只用背景 PostMessage
                        if j:
                            time.sleep(0.02)
                        post_key(target, key, hold=0.04)
                    time.sleep(0.08)
            except Exception as e:
                log_event(f"自動副手戰吼送鍵失敗: {e}")
            finally:
                self._offhand_busy = False

        threading.Thread(target=run, daemon=True).start()

    # ---------- 每局計時 / 防卡時間 ----------
    def update_round_label(self):
        """通關時間計時/防卡時間 (觸發次數); 通關後計時歸零, 超過防卡時間會變橘色"""
        fmt = DiscordNotifier._fmt_round
        sec = max(0, self.elapsed - self._last_clear_elapsed)
        n = self.worker.anti_count if self.worker is not None else 0
        over = False
        if self.cfg.get("anti_stuck_enabled", True):
            limit = float(self.cfg.get("anti_stuck_minutes", 10.0)) * 60
            over = sec >= limit
            txt = f"{fmt(sec)}/{fmt(limit)}　(觸發 {n} 次)"
        else:
            txt = f"{fmt(sec)}/關閉　(觸發 {n} 次)"
        self.lbl_round.setText(txt)
        self.lbl_round.setStyleSheet("color:#ff9f43;" if over else "")

    def bring_game_front(self):
        """把鎖定的遊戲視窗還原並移到最前 (遊戲不能縮到背景執行)"""
        hwnd = self._move_target()
        if hwnd is None:
            self.add_log("尚未鎖定遊戲視窗，無法顯示", "失敗")
            return
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            fg = win32gui.GetForegroundWindow()
            cur = win32api.GetCurrentThreadId()
            fg_thread = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
            attached = bool(fg_thread) and fg_thread != cur
            if attached:
                win32process.AttachThreadInput(fg_thread, cur, True)
            try:
                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    win32process.AttachThreadInput(fg_thread, cur, False)
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            log_event("顯示遊戲視窗在最前")
        except Exception as e:
            log_event(f"顯示遊戲視窗失敗: {e}")
            self.add_log(f"顯示遊戲視窗失敗: {e}", "失敗")

    # ---------- 視窗鎖定 ----------
    def refresh_windows(self):
        self.cmb_win.clear()
        wins = list_windows(exclude={int(self.winId())})
        wins.sort(key=lambda x: (WINDOW_TITLE.lower() not in x[1].lower(), x[1]))   # 符合關鍵字的排最前面
        for hwnd, title in wins:
            self.cmb_win.addItem(title, hwnd)

    def _set_locked(self, hwnd):
        self.hwnd = hwnd
        l, t, r, b = win32gui.GetClientRect(hwnd)
        self.lbl_win.setText(f"{win32gui.GetWindowText(hwnd)}  ({r}×{b})")
        self.badge_lock.setText("已鎖定")

    def lock_selected(self):
        hwnd = self.cmb_win.currentData()
        if hwnd and win32gui.IsWindow(hwnd):
            self._set_locked(hwnd)
        else:
            self.hwnd = None
            self.lbl_win.setText("請先在清單選擇遊戲視窗 (或按重新整理)")
            self.badge_lock.setText("未鎖定")

    def lock_countdown(self):
        """按下後 3 秒, 鎖定當時位於最前面的視窗 -> 先切到遊戲視窗並點一下"""
        self.lbl_win.setText("請在 3 秒內點一下遊戲視窗…")
        QTimer.singleShot(3000, self.lock_foreground)

    def lock_foreground(self):
        hwnd = win32gui.GetForegroundWindow()
        if hwnd and hwnd != int(self.winId()) and "控制台" not in win32gui.GetWindowText(hwnd):
            self._set_locked(hwnd)
        else:
            self.lbl_win.setText("沒有抓到遊戲視窗, 請再按一次並點遊戲")

    # ---------- 第二張圖種類 ----------
    def img2_changed(self):
        self.cfg["img2_variant"] = self.cmb_img2.currentData()
        save_config(self.cfg)
        self.img2_label()

    def img2_label(self):
        name = f"step2-{self.cfg['img2_variant']}.png"
        ok = find_image(name).exists()
        self.lbl_img2.setText(f"→ images/{name}  " + ("✔" if ok else "✖ 找不到檔案"))
        self.lbl_img2.setStyleSheet("color:#2ecc71;" if ok else "color:#ff6b6b;")

    # ---------- 按鍵流程編輯 ----------
    def seq_sync_suffix(self):
        self.spn_count.setSuffix(" 秒" if self.cmb_key.currentData() == "wait" else " 次")

    def seq_refresh(self, select=None, save=True):
        self.lst_seq.blockSignals(True)
        self.lst_seq.clear()
        for i, it in enumerate(self.cfg["key_sequence"], 1):
            self.lst_seq.addItem(f"{i}. {seq_item_text(it)}")
        if select is not None and 0 <= select < self.lst_seq.count():
            self.lst_seq.setCurrentRow(select)
        self.lst_seq.blockSignals(False)
        if save:
            save_config(self.cfg)

    def seq_pick(self, row):
        seq = self.cfg["key_sequence"]
        if 0 <= row < len(seq):
            it = seq[row]
            self.cmb_key.setCurrentIndex(max(0, self.cmb_key.findData(it["key"])))
            self.spn_count.setValue(it["count"])

    def seq_add(self):
        seq = self.cfg["key_sequence"]
        seq.append({"key": self.cmb_key.currentData(), "count": self.spn_count.value()})
        self.seq_refresh(select=len(seq) - 1)

    def seq_update(self):
        row = self.lst_seq.currentRow()
        if row < 0:
            return
        self.cfg["key_sequence"][row] = {"key": self.cmb_key.currentData(), "count": self.spn_count.value()}
        self.seq_refresh(select=row)

    def seq_delete(self):
        row = self.lst_seq.currentRow()
        seq = self.cfg["key_sequence"]
        if row < 0:
            return
        seq.pop(row)
        self.seq_refresh(select=min(row, len(seq) - 1))

    def seq_move(self, d):
        row = self.lst_seq.currentRow()
        seq = self.cfg["key_sequence"]
        new = row + d
        if row < 0 or not (0 <= new < len(seq)):
            return
        seq[row], seq[new] = seq[new], seq[row]
        self.seq_refresh(select=new)

    def cfg_changed(self):
        self.cfg["key_interval"] = round(self.spn_interval.value(), 2)
        self.cfg["key_hold"] = round(self.spn_hold.value(), 2)
        self.cfg["delay_after_img2"] = round(self.spn_delay.value(), 1)
        if hasattr(self, "chk_anti"):
            self.cfg["anti_stuck_enabled"] = self.chk_anti.isChecked()
            self.cfg["anti_stuck_minutes"] = round(self.spn_anti.value(), 1)
            self.cfg["idle_report_minutes"] = round(self.spn_idle.value(), 1)
        for cmb, spn, kn, cn in getattr(self, "_key_rows", ()):
            self.cfg[kn] = cmb.currentData()
            self.cfg[cn] = int(spn.value())
        if hasattr(self, "lbl_round"):
            self.update_round_label()
        save_config(self.cfg)

    # ---------- 控制 ----------
    def start(self):
        if self.state != "stopped":
            return
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            self.hwnd = None
            self.badge_lock.setText("未鎖定")
            self.lbl_win.setText("請先鎖定遊戲視窗")
            return
        skip = bool(self.cmb_fail_mode.currentData())
        bg = bool(self.cmb_op_mode.currentData())
        # 僅在前台模式才將遊戲視窗置頂；後台靜默模式不置頂，不搶焦點，不干擾使用者瀏覽其他網頁
        if not bg:
            win32gui.SetForegroundWindow(self.hwnd)
        if self._keep_time:
            self._keep_time = False         # 卡死自動重啟: 時間與統計照舊
        else:
            self.elapsed = 0
            self._last_clear_elapsed = 0
            self.run_rounds = 0
            self.lbl_avg.setText("--")
        for w in WATCHES:                   # 監看狀態先歸零, worker 載入圖片後會回報
            lab = self.lbl_watch.get(w["image"])
            if lab is not None:
                lab.setText(f"{w['name']}：待命")
                lab.setStyleSheet("color:#888;")
        self.worker = Worker(self.hwnd, skip_on_fail=skip, background_mode=bg, cfg=self.cfg)
        self.freeze.reset(self.hwnd, self.worker)
        self.worker.monitor = self.freeze
        self.worker.sig_status.connect(self.on_status)
        self.worker.sig_log.connect(self.add_log)
        self.worker.sig_result.connect(self.on_result)
        self.worker.sig_round.connect(self.on_round)
        self.worker.sig_death.connect(self.on_death)
        self.worker.sig_watch.connect(self.on_watch)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()
        self.timer.start(1000)
        self.update_round_label()
        self.state = "running"
        self._wd_reported = 0
        log_event("按下開始")
        self.badge_run.setText("執行中")
        self.btn_start.setEnabled(False); self.btn_pause.setEnabled(True); self.btn_stop.setEnabled(True)

    def toggle_pause(self):
        if self.state == "running":
            self.worker.pause(True); self.state = "paused"
            self.badge_run.setText("已暫停"); self.btn_pause.setText("▶ 繼續 (F7)")
        elif self.state == "paused":
            self.worker.pause(False); self.state = "running"
            self.badge_run.setText("執行中"); self.btn_pause.setText("⏸ 暫停 (F7)")

    def stop(self):
        if self.worker:
            log_event("按下停止")
            self.worker.stop()

    def on_finished(self):
        # 只處理「目前這個 worker」的結束 (卡死強制重啟時, 舊的 worker 可能比較晚才結束)
        if self.sender() is not self.worker:
            return
        self.timer.stop()
        self.state = "stopped"
        self.badge_run.setText("待命")
        self.btn_start.setEnabled(True); self.btn_pause.setEnabled(False); self.btn_stop.setEnabled(False)
        self.btn_pause.setText("⏸ 暫停 (F7)")
        self.update_income()
        for w in WATCHES:
            lab = self.lbl_watch.get(w["image"])
            if lab is not None and "監看中" in lab.text():
                lab.setText(f"{w['name']}：已停止")
                lab.setStyleSheet("color:#888;")
        log_event("流程停止 (worker 結束)")

    def on_status(self, name, img, nxt):
        self.lbl_target.setText(name)
        self.lbl_img.setText(f"比對圖片：{img}")
        self.lbl_next.setText(nxt)

    def on_watch(self, image, text):
        """step5 / step6 監看狀態顯示在「狀態」頁"""
        lab = self.lbl_watch.get(image)
        if lab is None:
            return
        name = next((w["name"] for w in WATCHES if w["image"] == image), image)
        lab.setText(f"{name}：{text}")
        if "監看中" in text:
            color = "#2ecc71"
        elif "偵測到" in text:
            color = "#f1c40f"
        elif "找不到" in text or "仍在" in text:
            color = "#ff6b6b"
        elif "已離開" in text:
            color = "#2ecc71"
        else:
            color = "#ddd"
        lab.setStyleSheet(f"color:{color};")

    def on_round(self):
        self.rounds += 1
        self.run_rounds += 1
        self.lbl_rounds.setText(f"{self.rounds:,}")
        self.add_log(f"關卡結束，累計第 {self.rounds} 關", "成功")
        round_sec = max(0, self.elapsed - self._last_clear_elapsed)   # 本局時間 = 這次通關 - 上次通關
        self._last_clear_elapsed = self.elapsed
        self.update_round_label()
        self.notifier.notify_clear(self.rounds, self.elapsed, round_sec)      # 與介面顯示的關卡數 / 時間一致
        self.update_avg()
        self.update_income()

    def reset_rounds(self):
        self.rounds = 0
        self.lbl_rounds.setText("0")
        self.lbl_avg.setText("--")
        self.update_income()

    def on_death(self):
        self.deaths += 1
        self.lbl_deaths.setText(f"{self.deaths:,}")
        self.add_log(f"偵測到死亡，累計第 {self.deaths} 次", "失敗")
        log_event(f"死亡次數: {self.deaths}")
        self.notifier.add_death()

    def reset_deaths(self):
        self.deaths = 0
        self.lbl_deaths.setText("0")

    def reset_time(self):
        """重設執行時間 (不影響關卡 / 死亡次數)"""
        self.elapsed = 0
        self._last_clear_elapsed = 0
        self.lbl_time.setText("00:00:00")
        self.update_round_label()
        self.update_avg()
        self.update_income()

    def open_log(self):
        """打開紀錄檔, 看看程式之前為什麼停掉 / 出錯"""
        try:
            log_event("使用者開啟紀錄檔")
            os.startfile(str(LOG_PATH))
        except Exception as e:
            self.add_log(f"無法開啟紀錄檔: {e}", "失敗")

    def _watchdog(self):
        """流程若長時間完全沒有動作 (卡在某個 Win32 呼叫), 先記錄, 太久就強制重啟"""
        w = self.worker
        if self.state == "stopped" or w is None:
            return
        if self.state == "paused" or not w.isRunning():
            return
        idle = time.time() - getattr(w, "beat", time.time())
        if idle < WATCH_IDLE_WARN:
            self._wd_reported = 0
            return
        if self._wd_reported == 0:
            self._wd_reported = 1
            self.add_log(f"流程已 {int(idle)} 秒沒有動作，持續監看…", "失敗")
            log_event(f"watchdog: 無心跳 {idle:.0f} 秒")
        if idle >= WATCH_IDLE_RESTART:
            self.add_log("流程疑似卡死，強制重新啟動", "失敗")
            log_event("watchdog: 強制重新啟動流程")
            self._restart_worker()

    def _restart_worker(self):
        """放棄卡住的 worker, 重新開一個 (舊的若醒來會看到停止旗標自行結束)"""
        old = self.worker
        if old is not None:
            try:
                old.sig_status.disconnect()
                old.sig_log.disconnect()
                old.sig_result.disconnect()
                old.sig_round.disconnect()
                old.sig_death.disconnect()
                old.finished.disconnect()
            except Exception:
                pass
            old.stop()
        self.worker = None
        self.state = "stopped"
        self._wd_reported = 0
        self._keep_time = True
        QTimer.singleShot(1500, self.start)

    def update_avg(self):
        if self.run_rounds > 0 and self.elapsed > 0:
            sec = self.elapsed / self.run_rounds
            self.lbl_avg.setText(f"{int(sec // 60):02d}:{int(sec % 60):02d}")

    def on_result(self, ok, text):
        self.lbl_result.setText(("✔ " if ok else "✖ ") + text)
        self.lbl_result.setStyleSheet("color:#2ecc71;" if ok else "color:#ff6b6b;")

    def tick(self):
        if self.state == "running":
            self.elapsed += 1
        h, rem = divmod(self.elapsed, 3600)
        m, s = divmod(rem, 60)
        self.lbl_time.setText(f"{h:02d}:{m:02d}:{s:02d}")
        # 去除動態每秒跳動的每分鐘收益計算，避免干擾；僅在填入金額時計算
        self.update_avg()
        self.update_round_label()
        self._watchdog()
        self.freeze.check(self.state == "running")

    # ---------- 金額 ----------
    @staticmethod
    def to_num(text):
        try:
            return float(text.replace(",", "").strip() or 0)
        except ValueError:
            return 0.0

    def update_income(self):
        """填入數字後計算 總收益、每場收益 與 每分鐘收益"""
        total = self.to_num(self.in_end.text()) - self.to_num(self.in_start.text())
        
        # 1. 總收益
        self.lbl_total.setText(f"{'+' if total >= 0 else '-'}{abs(total):,.0f}")
        self.lbl_total.setStyleSheet("color:#2ecc71;" if total >= 0 else "color:#ff6b6b;")

        # 2. 每場收益 (總收益 / 已刷關卡次數)
        if self.rounds > 0:
            per_round = total / self.rounds
            self.lbl_per_round.setText(f"{'+' if per_round >= 0 else '-'}{abs(per_round):,.1f}")
            self.lbl_per_round.setStyleSheet("color:#2ecc71;" if per_round >= 0 else "color:#ff6b6b;")
        else:
            self.lbl_per_round.setText("--")
            self.lbl_per_round.setStyleSheet("color:#ddd;")

        # 3. 每分鐘收益 (靜態計算)
        minutes = self.elapsed / 60
        if minutes > 0:
            pm = total / minutes
            self.lbl_pm.setText(f"{'+' if pm >= 0 else '-'}{abs(pm):,.2f}")
            self.lbl_pm.setStyleSheet("color:#2ecc71;" if pm >= 0 else "color:#ff6b6b;")
        else:
            self.lbl_pm.setText("0.00")
            self.lbl_pm.setStyleSheet("color:#ddd;")

    # ---------- 熱鍵 (選用) ----------
    def setup_hotkeys(self):
        try:
            import keyboard
        except Exception:
            return   # 沒裝 keyboard 就只用按鈕
        # keyboard 的 callback 跑在它自己的執行緒, 不能直接碰 Qt 物件;
        # 用 Signal 丟回主執行緒處理 (舊版用 QTimer.singleShot 有機會讓程式直接消失)
        self._hk = HotkeyBridge()
        self._hk.triggered.connect(self._on_hotkey)
        try:
            keyboard.add_hotkey("f6", lambda: self._hk.triggered.emit("f6"))
            keyboard.add_hotkey("f7", lambda: self._hk.triggered.emit("f7"))
            keyboard.add_hotkey("f8", lambda: self._hk.triggered.emit("f8"))
        except Exception as e:
            log_event(f"熱鍵註冊失敗: {e}")

    def _on_hotkey(self, key):
        if key == "f6":
            self.start()
        elif key == "f7":
            self.toggle_pause()
        elif key == "f8":
            self.stop()

    def closeEvent(self, e):
        log_event("關閉程式")
        self.offhand_timer.stop()
        self.move_release_all()
        self.stop()
        if self.worker is not None:
            self.worker.wait(2000)      # 給執行緒一點時間收尾, 避免關閉時崩潰
        e.accept()


if __name__ == "__main__":
    if "--test-discord" in sys.argv:            # python d2_survivors_auto.py --test-discord
        _n = DiscordNotifier()
        _st = _n.status_text()
        ok = _st == "OK" and _n._post("🔔 D2Auto Discord 測試訊息 (卡死通知會像這樣 TAG 你)", True, False)
        _msg = "成功" if ok else f"失敗: {_st if _st != 'OK' else '發送被拒絕, 詳見 d2auto_log.txt'}"
        log_event(f"Discord 測試: {_msg}")
        print(_msg)
        sys.exit(0)
    hide_console_window()
    IMAGE_DIR.mkdir(exist_ok=True)
    log_event("=" * 20 + " 程式啟動 " + "=" * 20)
    app = QApplication(sys.argv)
    w = Main(); w.show()
    sys.exit(app.exec())