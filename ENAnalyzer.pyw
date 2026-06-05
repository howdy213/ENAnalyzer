#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ENAnalyzer - 希沃白板课件自动打包与管理工具
支持完整性校验、按账户分类存储、自动转存、连续失败跳过等功能。
-silent 启动参数，静默运行不显示主窗口，仅驻留系统托盘。
"""

import os
import json
import shutil
import zipfile
import time
import threading
import subprocess
import ctypes
import re
import sys
import wx
import wx.adv
import hashlib
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

# 全局常量
APP_NAME = "ENAnalyzer"
VERSION = "1.0.0"
SILENT_MODE = "-silent" in sys.argv

def get_system_root():
    """返回系统盘根目录，例如 C:\\"""
    drive = os.environ.get('SystemDrive', 'C:')
    return drive + '\\'

def get_current_user_name():
    """
    调用 Windows API GetUserNameW 获取当前登录用户名，返回字符串。
    失败时回退到环境变量 USERNAME。
    """
    try:
        buf = ctypes.create_unicode_buffer(256)
        size = ctypes.c_ulong(len(buf))
        if ctypes.windll.advapi32.GetUserNameW(buf, ctypes.byref(size)):
            return buf.value
    except Exception:
        pass
    return os.environ.get('USERNAME', '')

def get_active_user_profile():
    """
    获取实际活动用户目录。
    """
    username = get_current_user_name()
    if username.upper() != 'SYSTEM':
        user_profile = os.path.join(get_system_root(), 'Users', username)
        return user_profile
    else:
        os._exit(0)

def base_dir():
    """获取应用程序根目录"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

# 配置文件与日志路径
CONFIG_DIR = os.path.join(base_dir(), "config")
PACK_LOG_PATH = os.path.join(CONFIG_DIR, "pack_history.json")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
_user_profile = get_active_user_profile()
DEFAULT_OUTPUT_DIR = os.path.join(_user_profile, "Documents", "ENAnalyzer_Backup")
DEFAULT_DATA_DIR = os.path.join(_user_profile, 'AppData', 'Roaming', 'Seewo', 'EasiNote5', 'Data')

def sanitize_filename(name, default='untitled'):
    """清理文件名中的非法字符，限制长度"""
    if not name:
        name = default
    name = re.sub(r'[\\/*?:"<>|]', '_', name).strip(' .')
    return name[:200] if name else default

def parse_courseware_json(json_path):
    """解析希沃课件 JSON 文件，返回 (课件信息字典, 文件列表)"""
    try:
        with open(json_path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
    except Exception:
        return None, []
    return data.get('Courseware', {}), data.get('CoursewareFiles', [])

def validate_files(files, courseware_dir, enable_checksum=True):
    """
    校验文件完整性：存在性、下载完成标志、大小、哈希（可选）
    返回无效文件列表 [(文件名, 原因), ...]
    """
    invalid = []
    for f in files:
        filename = f['FileName']
        filepath = os.path.join(courseware_dir, filename)
        if not os.path.exists(filepath):
            invalid.append((filename, "文件不存在"))
            continue
        partial_state = f.get('FilePartialState', 0)
        if partial_state != 0:
            invalid.append((filename, f"文件未下载完全 (PartialState={partial_state})"))
            continue
        expected_size = f.get('FileSize', -1)
        if expected_size != -1:
            actual_size = os.path.getsize(filepath)
            if actual_size != expected_size:
                invalid.append((filename, f"文件大小不匹配 (期望{expected_size}, 实际{actual_size})"))
                continue
        if enable_checksum:
            expected_hash = f.get('FileHash', '')
            if expected_hash:
                hasher = hashlib.md5()
                try:
                    with open(filepath, 'rb') as fh:
                        while True:
                            chunk = fh.read(65536)
                            if not chunk:
                                break
                            hasher.update(chunk)
                    actual_hash = hasher.hexdigest()
                    if actual_hash.lower() != expected_hash.lower():
                        invalid.append((filename, "文件哈希不匹配"))
                except Exception as e:
                    invalid.append((filename, f"读取文件出错: {e}"))
    return invalid

def generate_config_xml(files, output_dir):
    """生成 Open XML 格式的 [Content_Types].xml 文件"""
    extensions = set()
    overrides = []
    for item in files:
        fname = item['FileName']
        rel = item['RelativePath']
        if '.' in fname:
            ext = fname.rsplit('.', 1)[-1]
            if ext.lower() != 'xml':
                extensions.add(ext)
        overrides.append('/' + rel.replace('\\', '/'))
    ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
    root = Element('Types', xmlns=ns)
    for ext in sorted(extensions):
        SubElement(root, 'Default', Extension=ext, ContentType='')
    for part in overrides:
        SubElement(root, 'Override', PartName=part, ContentType='')
    xml_bytes = minidom.parseString(tostring(root, 'utf-8')).toprettyxml(indent='  ', encoding='utf-8')
    xml_str = xml_bytes.decode('utf-8')
    output = '\n'.join([line for line in xml_str.splitlines() if line.strip()])
    xml_path = os.path.join(output_dir, '[Content_Types].xml')
    with open(xml_path, 'w', encoding='utf-8') as f:
        f.write(output)
    return xml_path

def pack_courseware(courseware_dir, json_name, output_path, progress_callback=None):
    """
    将课件目录打包为 ENBX 文件（ZIP 格式），包含文件复制、XML 生成、压缩
    """
    json_path = os.path.join(courseware_dir, json_name)
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"JSON 文件不存在: {json_path}")
    info, files = parse_courseware_json(json_path)
    if info is None:
        raise ValueError("JSON 解析失败")
    temp_dir = os.path.join(os.path.dirname(output_path), f"__temp_{time.time_ns()}")
    os.makedirs(temp_dir, exist_ok=True)
    # 复制文件到临时目录
    for idx, f in enumerate(files):
        src = os.path.join(courseware_dir, f['FileName'])
        dst = os.path.join(temp_dir, f['RelativePath'].replace('/', os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            if progress_callback:
                wx.CallAfter(progress_callback, f"⚠️ {f['FileName']} 复制失败: {e}")
        if progress_callback:
            wx.CallAfter(progress_callback, int((idx + 1) / len(files) * 50))
    generate_config_xml(files, temp_dir)
    # 打包为 ZIP
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _, files_in in os.walk(temp_dir):
            for fn in files_in:
                abs_path = os.path.join(root, fn)
                zf.write(abs_path, os.path.relpath(abs_path, temp_dir))
    shutil.rmtree(temp_dir, ignore_errors=True)
    if progress_callback:
        wx.CallAfter(progress_callback, 100)
    return output_path

def scan_data_dir(data_dir):
    """
    扫描希沃数据目录，返回结构：
    { 账户ID: { 课件ID: (课件信息, 文件列表, 课件目录路径), ... }, ... }
    """
    result = {}
    if not os.path.isdir(data_dir):
        return result
    for acc in os.listdir(data_dir):
        acc_dir = os.path.join(data_dir, acc)
        if not os.path.isdir(acc_dir):
            continue
        cw_root = os.path.join(acc_dir, 'Courseware')
        if not os.path.isdir(cw_root):
            continue
        cw_dict = {}
        for cid in os.listdir(cw_root):
            cid_dir = os.path.join(cw_root, cid)
            if not os.path.isdir(cid_dir):
                continue
            json_path = os.path.join(cid_dir, f"{cid}.json")
            if os.path.isfile(json_path):
                info, files = parse_courseware_json(json_path)
                if info is not None:
                    cw_dict[cid] = (info, files, cid_dir)
        if cw_dict:
            result[acc] = cw_dict
    return result

# ----- 设置与日志读写函数 -----
def load_settings():
    """加载程序设置，提供默认值"""
    if not os.path.exists(SETTINGS_PATH):
        return {
            "auto_pack": False,
            "monitor_dir": DEFAULT_DATA_DIR,
            "output_root": DEFAULT_OUTPUT_DIR,
            "by_account": True,
            "account_paths": {},
            "skip_cids": [],
            "enable_hash_check": True,
            "fail_counts": {}
        }
    with open(SETTINGS_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
        data.setdefault("fail_counts", {})
        data.setdefault("account_paths", {})
        data.setdefault("skip_cids", [])
        data.setdefault("enable_hash_check", True)
        return data

def save_settings(settings):
    """保存设置到文件"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2)

def load_pack_log():
    """加载打包历史记录"""
    if not os.path.exists(PACK_LOG_PATH):
        return []
    with open(PACK_LOG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_pack_log(log):
    """保存打包历史记录"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(PACK_LOG_PATH, 'w', encoding='utf-8') as f:
        json.dump(log, f, indent=2)

class SquareButton(wx.Button):
    """保证按钮始终为正方形的按钮类"""
    def __init__(self, parent, label, **kwargs):
        if 'style' not in kwargs:
            kwargs['style'] = 0
        super().__init__(parent, label=label, **kwargs)

        dc = wx.ClientDC(self)
        dc.SetFont(self.GetFont())
        text_size = dc.GetMultiLineTextExtent(label)
        edge = text_size.height + 8

        self.SetMinSize((edge, edge))
        self.SetMaxSize((edge, edge))
        self.SetSize((edge, edge))

class ToastNotification(wx.PopupTransientWindow):
    """可撤销操作的浮动通知窗口，带撤销按钮，超时自动关闭"""
    def __init__(self, parent, message, on_undo, timeout=10):
        super().__init__(parent, flags=wx.BORDER_SIMPLE)
        self.on_undo = on_undo
        self.timer = wx.Timer(self)
        self.timeout = timeout

        panel = wx.Panel(self)
        sz = wx.BoxSizer(wx.HORIZONTAL)

        btn_undo = SquareButton(panel, label="↩")
        btn_undo.Bind(wx.EVT_BUTTON, self.on_undo_click)
        sz.Add(btn_undo, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)

        msg_text = wx.StaticText(panel, label=message, style=wx.ST_ELLIPSIZE_END)
        msg_text.Wrap(300)
        sz.Add(msg_text, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)

        btn_close = SquareButton(panel, label="❎️", style=wx.BORDER_NONE)
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.Dismiss())
        sz.Add(btn_close, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)

        panel.SetSizer(sz)
        panel.Layout()

        txt_w = msg_text.GetBestSize().width
        btn_h = btn_undo.GetBestSize().height
        total_w = btn_h*2 + txt_w + 30 + 60   # 两个正方形按钮 + 文本 + 间隔
        total_w = max(total_w, 300)
        screen_w, screen_h = wx.DisplaySize()
        total_w = min(total_w, screen_w - 40)
        total_h = max(msg_text.GetBestSize().height, btn_h) + 20
        self.SetSize((total_w, total_h))
        panel.SetSize((total_w, total_h))

        self.Bind(wx.EVT_TIMER, self.on_timer, self.timer)
        self.timer.Start(1000)

        x = (screen_w - total_w) // 2
        y = screen_h - total_h - 50
        self.SetPosition((x, y))
        self.Show()

    def on_undo_click(self, event):
        self.timer.Stop()
        self.on_undo()
        self.Dismiss()

    def on_timer(self, event):
        self.timeout -= 1
        if self.timeout <= 0:
            self.Dismiss()

    def Dismiss(self):
        self.timer.Stop()
        super().Dismiss()

class TaskBarIcon(wx.adv.TaskBarIcon):
    """系统托盘图标，左键显示主窗口，右键菜单显示/退出"""
    def __init__(self, frame):
        super().__init__()
        self.frame = frame
        icon_path = os.path.join(base_dir(), "asset", "ENAnalyzer.ico")
        if os.path.exists(icon_path):
            icon = wx.Icon(icon_path, wx.BITMAP_TYPE_ICO)
        else:
            icon = wx.Icon(wx.ArtProvider.GetBitmap(wx.ART_FOLDER, wx.ART_OTHER, (16, 16)))
        self.SetIcon(icon, APP_NAME)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DOWN, self.on_left_click)

    def CreatePopupMenu(self):
        menu = wx.Menu()
        item_show = menu.Append(-1, "显示主窗口")
        menu.Bind(wx.EVT_MENU, self.on_show, item_show)
        item_exit = menu.Append(-1, "退出")
        menu.Bind(wx.EVT_MENU, self.on_exit, item_exit)
        return menu

    def on_left_click(self, event):
        self.frame.Show()
        self.frame.Raise()

    def on_show(self, event):
        self.frame.Show()
        self.frame.Raise()

    def on_exit(self, event):
        self.frame.on_real_exit(None)

class MainFrame(wx.Frame):
    """主窗口，包含所有UI界面和业务逻辑"""
    def __init__(self):
        super().__init__(None, title=APP_NAME, size=(1050, 750))
        self.settings = load_settings()
        self.pack_log = load_pack_log()
        self.courseware_data = {}
        self.fail_counts = self.settings.get("fail_counts", {})
        self.fail_lock = threading.Lock()

        # 开机自启动服务路径
        self.auto_start_exe = os.path.join(base_dir(), "tools", "AutoStartService.exe")
        self.has_service_exe = os.path.isfile(self.auto_start_exe)

        # 程序图标
        icon_path = os.path.join(base_dir(), "asset", "ENAnalyzer.ico")
        if os.path.exists(icon_path):
            self.SetIcon(wx.Icon(icon_path, wx.BITMAP_TYPE_ICO))

        # 初始化各种路径与状态
        self.data_dir = DEFAULT_DATA_DIR
        self.auto_pack_enabled = self.settings.get("auto_pack", False)
        self.monitor_dir = self.settings.get("monitor_dir", self.data_dir)
        self.output_root = self.settings.get("output_root", DEFAULT_OUTPUT_DIR)
        self.by_account = self.settings.get("by_account", True)
        self.account_paths = self.settings.get("account_paths", {})
        self.skip_cids = set(self.settings.get("skip_cids", []))

        # 创建 Notebook 标签页
        self.notebook = wx.Notebook(self)
        self.home_panel = wx.Panel(self.notebook)
        self.mgmt_panel = wx.Panel(self.notebook)
        self.log_panel = wx.Panel(self.notebook)
        self.settings_panel = wx.Panel(self.notebook)
        self.about_panel = wx.Panel(self.notebook)

        self.notebook.AddPage(self.home_panel, "首页")
        self.notebook.AddPage(self.mgmt_panel, "课件管理")
        self.notebook.AddPage(self.log_panel, "打包记录")
        self.notebook.AddPage(self.settings_panel, "转存设置")
        self.notebook.AddPage(self.about_panel, "关于")

        # 初始化各页面
        self.init_home_page()
        self.init_mgmt_page()
        self.init_log_page()
        self.init_settings_page()
        self.init_about_page()

        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.tray_icon = TaskBarIcon(self)

        # 自动监控定时器
        self.monitor_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_monitor_tick, self.monitor_timer)
        if self.auto_pack_enabled:
            self.monitor_timer.Start(10000)

        self.refresh_mgmt()
        self.Center()

        # 根据 SILENT_MODE 决定是否显示主窗口
        if not SILENT_MODE:
            self.Show()

    # ---------- 管理员权限相关辅助方法 ----------
    @staticmethod
    def is_admin():
        """判断当前进程是否以管理员身份运行"""
        try:
            return ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            return False

    def run_as_admin(self, exe_path, params, wait=True):
        """以管理员身份运行程序，使用宽字符参数避免路径编码问题。"""
        class SHELLEXECUTEINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("fMask", ctypes.c_ulong),
                ("hwnd", ctypes.c_void_p),
                ("lpVerb", ctypes.c_wchar_p),
                ("lpFile", ctypes.c_wchar_p),
                ("lpParameters", ctypes.c_wchar_p),
                ("lpDirectory", ctypes.c_wchar_p),
                ("nShow", ctypes.c_int),
                ("hInstApp", ctypes.c_void_p),
                ("lpIDList", ctypes.c_void_p),
                ("lpClass", ctypes.c_wchar_p),
                ("hkeyClass", ctypes.c_void_p),
                ("dwHotKey", ctypes.c_ulong),
                ("hIcon", ctypes.c_void_p),
                ("hProcess", ctypes.c_void_p)
            ]

        SEE_MASK_NOCLOSEPROCESS = 0x00000040
        SW_HIDE = 0

        sei = SHELLEXECUTEINFO()
        sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFO)
        sei.fMask = SEE_MASK_NOCLOSEPROCESS
        sei.lpVerb = "runas"
        sei.lpFile = exe_path
        sei.lpParameters = params if params else ""
        sei.lpDirectory = None
        sei.nShow = SW_HIDE

        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
            raise RuntimeError(f"ShellExecuteEx 调用失败 (Error: {ctypes.GetLastError()})")

        if wait and sei.hProcess:
            INFINITE = 0xFFFFFFFF
            ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, INFINITE)
            exit_code = ctypes.c_ulong(0)
            ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(sei.hProcess)
            return exit_code.value
        return 0

    @staticmethod
    def _safe_str(val):
        return str(val) if val is not None else ""

    def _get_courseware_file_mtime(self, cid_dir):
        """获取课件根目录下直接文件的最大修改时间"""
        max_time = 0.0
        try:
            for item in os.listdir(cid_dir):
                full_path = os.path.join(cid_dir, item)
                if os.path.isfile(full_path):
                    try:
                        t = os.path.getmtime(full_path)
                        if t > max_time:
                            max_time = t
                    except OSError:
                        continue
        except Exception:
            pass
        return max_time

    # ---------- 首页 ----------
    def init_home_page(self):
        self.home_scroll = wx.ScrolledWindow(self.home_panel)
        self.home_scroll.SetScrollRate(5, 5)
        self.home_scroll.SetBackgroundColour(wx.Colour(245, 245, 245))
        self.home_panel.SetSizer(wx.BoxSizer(wx.VERTICAL))
        self.home_panel.GetSizer().Add(self.home_scroll, 1, wx.EXPAND)

    def refresh_home_page(self):
        """刷新首页，显示最多6个课件卡片，3×2网格，按文件修改时间排序，自动适应宽度"""
        self.home_scroll.DestroyChildren()
        items = []
        for acc, cw_dict in self.courseware_data.items():
            for cid, (info, _, cid_dir) in cw_dict.items():
                json_mtime = info.get('UpdateTime', 0) / 1000.0       # 状态判断用
                file_mtime = self._get_courseware_file_mtime(cid_dir) # 显示及排序用
                size = sum(
                    os.path.getsize(os.path.join(cid_dir, f))
                    for f in os.listdir(cid_dir)
                    if os.path.isfile(os.path.join(cid_dir, f))
                )
                status, color = self.get_pack_status(acc, cid, json_mtime)
                items.append({
                    'acc': acc, 'cid': cid,
                    'name': self._safe_str(info.get('Name')),
                    'author': self._safe_str(info.get('Author')),
                    'mtime': json_mtime,       # 内部使用，不直接显示
                    'file_mtime': file_mtime,  # 显示用
                    'size': size,
                    'status': status, 'color': color
                })
        items.sort(key=lambda x: x['file_mtime'], reverse=True)
        top6 = items[:6]

        # 计算可用宽度，用于限制卡片最大宽度
        self.home_scroll.GetParent().Layout()  # 强制更新布局以获取准确尺寸
        client_width = self.home_scroll.GetClientSize().width
        if client_width <= 0:
            client_width = 800  # 默认值
        # 3 列网格，左右留白，列间距 10px
        col_width = (client_width - 30) // 3   # 30 为左右边距总和 (15+15)
        if col_width < 200:
            col_width = 200  # 最小宽度

        sz = wx.BoxSizer(wx.VERTICAL)
        welcome = wx.StaticText(self.home_scroll, label=f"欢迎使用 {APP_NAME}")
        font = welcome.GetFont()
        font.PointSize += 5
        font = font.Bold()
        welcome.SetFont(font)
        welcome.SetForegroundColour(wx.Colour(0, 120, 215))
        sz.Add(welcome, 0, wx.ALIGN_CENTER | wx.ALL, 15)

        if not top6:
            sz.Add(wx.StaticText(self.home_scroll, label="暂无课件数据"), 0, wx.ALL, 10)
        else:
            grid_sz = wx.FlexGridSizer(rows=2, cols=3, vgap=10, hgap=10)
            for i in range(3):
                grid_sz.AddGrowableCol(i, 1)

            for item in top6:
                card = wx.Panel(self.home_scroll)
                card.SetBackgroundColour(wx.WHITE)
                card.SetWindowStyleFlag(wx.BORDER_SIMPLE)
                card.SetMaxSize((col_width, -1))          # 限制卡片最大宽度
                card_sz = wx.BoxSizer(wx.VERTICAL)

                title = wx.StaticText(card, label=item['name'])
                title.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
                title.Wrap(col_width - 20)                # 标题根据卡片宽度换行
                card_sz.Add(title, 0, wx.ALL | wx.EXPAND, 8)

                grid = wx.FlexGridSizer(cols=2, vgap=4, hgap=15)
                # 使用 file_mtime 显示文件实际修改时间
                pairs = [
                    ("账户", item['acc']),
                    ("作者", item['author']),
                    ("修改时间", time.strftime("%Y-%m-%d %H:%M", time.localtime(item['file_mtime']))),
                    ("大小", f"{item['size']/1024:.1f} KB")
                ]
                for label, value in pairs:
                    lbl = wx.StaticText(card, label=label + ":")
                    lbl.SetForegroundColour(wx.Colour(100, 100, 100))
                    val = wx.StaticText(card, label=value)
                    grid.Add(lbl, 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
                    grid.Add(val, 0, wx.ALIGN_LEFT | wx.ALIGN_CENTER_VERTICAL)
                card_sz.Add(grid, 0, wx.ALL | wx.EXPAND, 8)

                status_sz = wx.BoxSizer(wx.HORIZONTAL)
                status_lbl = wx.StaticText(card, label="状态:")
                status_lbl.SetForegroundColour(wx.Colour(100, 100, 100))
                status_sz.Add(status_lbl, 0, wx.ALIGN_CENTER_VERTICAL)
                indicator = wx.Panel(card, size=(12, 12))
                indicator.SetBackgroundColour(item['color'])
                status_sz.Add(indicator, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5)
                status_text = wx.StaticText(card, label=item['status'])
                status_text.SetForegroundColour(item['color'])
                status_sz.Add(status_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5)
                card_sz.Add(status_sz, 0, wx.ALL | wx.EXPAND, 8)

                card.SetSizer(card_sz)
                grid_sz.Add(card, 0, wx.EXPAND | wx.ALL, 0)

            # 不足 6 个时填充空白
            for _ in range(len(top6), 6):
                grid_sz.Add(wx.Panel(self.home_scroll), 0, wx.EXPAND)

            sz.Add(grid_sz, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.home_scroll.SetSizer(sz)
        self.home_scroll.Layout()
        self.home_scroll.SetVirtualSize(sz.GetMinSize())

    # ---------- 课件管理页面 ----------
    def init_mgmt_page(self):
        main_sz = wx.BoxSizer(wx.VERTICAL)
        hdir = wx.BoxSizer(wx.HORIZONTAL)
        hdir.Add(wx.StaticText(self.mgmt_panel, label="Data 目录:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.mgmt_dir_text = wx.TextCtrl(self.mgmt_panel, value=self.data_dir, size=(400, -1))
        hdir.Add(self.mgmt_dir_text, 0, wx.ALL, 5)
        btn_browse = wx.Button(self.mgmt_panel, label="浏览...")
        btn_browse.Bind(wx.EVT_BUTTON, self.on_mgmt_browse)
        hdir.Add(btn_browse, 0, wx.ALL, 5)
        btn_refresh = wx.Button(self.mgmt_panel, label="刷新")
        btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self.refresh_mgmt())
        hdir.Add(btn_refresh, 0, wx.ALL, 5)
        main_sz.Add(hdir, 0, wx.EXPAND | wx.ALL, 5)

        htools = wx.BoxSizer(wx.HORIZONTAL)
        htools.Add(wx.StaticText(self.mgmt_panel, label="搜索:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.mgmt_search = wx.TextCtrl(self.mgmt_panel, size=(200, -1))
        self.mgmt_search.Bind(wx.EVT_TEXT, self.on_mgmt_search)
        htools.Add(self.mgmt_search, 0, wx.ALL, 5)
        # 操作按钮行
        for label, handler in [("全选", self.on_mgmt_select_all), ("取消全选", self.on_mgmt_unselect_all),
                               ("标记已打包", self.on_mark_packed), ("标记为跳过", self.on_mark_skip),
                               ("还原状态", self.on_unmark_skip), ("删除选中", self.on_delete_selected)]:
            btn = wx.Button(self.mgmt_panel, label=label)
            btn.Bind(wx.EVT_BUTTON, handler)
            htools.Add(btn, 0, wx.ALL, 5)
        main_sz.Add(htools, 0, wx.EXPAND | wx.ALL, 5)

        self.mgmt_list = wx.ListCtrl(self.mgmt_panel, style=wx.LC_REPORT | wx.LC_HRULES)
        for col, width in [("选择", 50), ("账户", 120), ("课件名称", 250), ("作者", 100),
                           ("修改时间", 150), ("大小", 80), ("打包状态", 100)]:
            self.mgmt_list.InsertColumn(self.mgmt_list.GetColumnCount(), col, width=width)
        self.mgmt_list.Bind(wx.EVT_LIST_COL_CLICK, self.on_mgmt_col_click)
        self.mgmt_list.Bind(wx.EVT_LEFT_DOWN, self.on_mgmt_left_click)
        main_sz.Add(self.mgmt_list, 1, wx.EXPAND | wx.ALL, 10)

        hpack = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_pack = wx.Button(self.mgmt_panel, label="打包选中的课件")
        self.btn_pack.Bind(wx.EVT_BUTTON, self.on_pack_selected)
        hpack.Add(self.btn_pack, 0, wx.ALL, 5)
        self.mgmt_gauge = wx.Gauge(self.mgmt_panel, range=100, size=(-1, 20))
        hpack.Add(self.mgmt_gauge, 1, wx.ALL, 5)
        main_sz.Add(hpack, 0, wx.EXPAND | wx.ALL, 5)

        self.mgmt_panel.SetSizer(main_sz)
        self.mgmt_data = []
        self.mgmt_selected = set()
        self.mgmt_sort_col = None
        self.mgmt_sort_asc = True

    def refresh_mgmt(self, filter_text=""):
        """扫描数据目录，刷新课件管理列表"""
        self.mgmt_selected.clear()
        self.mgmt_list.DeleteAllItems()
        self.mgmt_data.clear()
        data_dir = self.mgmt_dir_text.GetValue().strip()
        if not os.path.isdir(data_dir):
            wx.MessageBox("Data 目录无效", "错误", wx.ICON_ERROR)
            return
        try:
            self.courseware_data = scan_data_dir(data_dir)
        except Exception as e:
            wx.MessageBox(f"扫描失败: {e}", "错误", wx.ICON_ERROR)
            return
        filter_text = filter_text.lower()
        for acc, cw_dict in self.courseware_data.items():
            for cid, (info, files, cid_dir) in cw_dict.items():
                name = self._safe_str(info.get('Name'))
                if filter_text and filter_text not in name.lower():
                    continue
                author = self._safe_str(info.get('Author'))
                mtime = info.get('UpdateTime', 0) / 1000.0
                size = sum(os.path.getsize(os.path.join(cid_dir, f)) for f in os.listdir(cid_dir) if os.path.isfile(os.path.join(cid_dir, f)))
                status = self.get_pack_status(acc, cid, mtime)
                self.mgmt_data.append({
                    'acc': acc, 'cid': cid, 'info': info, 'files': files, 'dir': cid_dir,
                    'name': name, 'author': author, 'mtime': mtime, 'size': size, 'status': status
                })
        self.sort_mgmt_data()
        self.repopulate_mgmt_list()
        self.refresh_home_page()

    def get_pack_status(self, acc, cid, json_mtime):
        """根据打包记录和跳过列表，返回(状态文本, 颜色)"""
        if f"{acc}|{cid}" in self.skip_cids:
            for rec in self.pack_log:
                if rec['account'] == acc and rec['cid'] == cid and rec.get('undo'):
                    return ("已撤销", wx.Colour(128, 128, 128))
            return ("已跳过", wx.Colour(128, 128, 128))
        latest = None
        for rec in self.pack_log:
            if rec['account'] == acc and rec['cid'] == cid:
                if latest is None or rec['pack_time'] > latest['pack_time']:
                    latest = rec
        if latest is None:
            return ("未打包", wx.RED)
        if latest.get("skipped"):
            return ("已撤销", wx.Colour(128, 128, 128))
        return ("有更新" if json_mtime > latest['pack_time'] / 1000.0 else "已打包",
                wx.GREEN if json_mtime > latest['pack_time'] / 1000.0 else wx.BLACK)

    def sort_mgmt_data(self):
        if self.mgmt_sort_col is None:
            return
        col_map = {2: 'name', 3: 'author', 4: 'mtime', 5: 'size'}
        col_key = col_map.get(self.mgmt_sort_col)
        if col_key:
            self.mgmt_data.sort(key=lambda x: x[col_key], reverse=not self.mgmt_sort_asc)

    def repopulate_mgmt_list(self):
        self.mgmt_list.DeleteAllItems()
        for idx, item in enumerate(self.mgmt_data):
            row = self.mgmt_list.InsertItem(idx, "√" if (item['acc'], item['cid']) in self.mgmt_selected else "")
            self.mgmt_list.SetItem(row, 1, item['acc'])
            self.mgmt_list.SetItem(row, 2, item['name'])
            self.mgmt_list.SetItem(row, 3, item['author'])
            self.mgmt_list.SetItem(row, 4, time.strftime("%Y-%m-%d %H:%M", time.localtime(item['mtime'])))
            self.mgmt_list.SetItem(row, 5, f"{item['size']/1024:.1f} KB")
            status_text, color = item['status']
            self.mgmt_list.SetItem(row, 6, status_text)
            self.mgmt_list.SetItemTextColour(row, color)

    # 列表交互事件
    def on_mgmt_col_click(self, event):
        col = event.GetColumn()
        if col not in (2, 3, 4, 5):
            return
        if self.mgmt_sort_col == col:
            self.mgmt_sort_asc = not self.mgmt_sort_asc
        else:
            self.mgmt_sort_col, self.mgmt_sort_asc = col, True
        self.sort_mgmt_data()
        self.repopulate_mgmt_list()

    def on_mgmt_left_click(self, event):
        x, y = event.GetPosition()
        row, _ = self.mgmt_list.HitTest((x, y))
        if row < 0:
            event.Skip()
            return
        if self._get_mgmt_clicked_col(x) == 0:
            selected_rows = [r for r in range(self.mgmt_list.GetItemCount()) if self.mgmt_list.IsSelected(r)]
            target_rows = selected_rows if (row in selected_rows and len(selected_rows) > 1) else [row]
            for r in target_rows:
                acc = self.mgmt_list.GetItemText(r, 1)
                cid = self.mgmt_data[r]['cid']
                if (acc, cid) in self.mgmt_selected:
                    self.mgmt_selected.discard((acc, cid))
                    self.mgmt_list.SetItem(r, 0, "")
                else:
                    self.mgmt_selected.add((acc, cid))
                    self.mgmt_list.SetItem(r, 0, "√")
            for r in range(self.mgmt_list.GetItemCount()):
                self.mgmt_list.Select(r, r in target_rows)
        else:
            event.Skip()

    @staticmethod
    def _get_mgmt_clicked_col(x_pos):
        col_widths = [50, 120, 250, 100, 150, 80, 100]
        cur = 0
        for i, w in enumerate(col_widths):
            if x_pos < cur + w:
                return i
            cur += w
        return -1

    def on_mgmt_search(self, event):
        self.refresh_mgmt(self.mgmt_search.GetValue())

    def on_mgmt_select_all(self, event):
        for idx, item in enumerate(self.mgmt_data):
            self.mgmt_selected.add((item['acc'], item['cid']))
            self.mgmt_list.SetItem(idx, 0, "√")

    def on_mgmt_unselect_all(self, event):
        for idx, item in enumerate(self.mgmt_data):
            self.mgmt_selected.discard((item['acc'], item['cid']))
            self.mgmt_list.SetItem(idx, 0, "")

    def on_mark_packed(self, event):
        if not self.mgmt_selected:
            wx.MessageBox("请先勾选课件", "提示", wx.ICON_INFORMATION)
            return
        for acc, cid in self.mgmt_selected:
            item = next((d for d in self.mgmt_data if d['acc'] == acc and d['cid'] == cid), None)
            if item:
                self.pack_log.append({"account": acc, "cid": cid, "name": item['name'],
                                      "pack_time": int(time.time() * 1000), "output_path": ""})
        save_pack_log(self.pack_log)
        self.refresh_mgmt()
        self.refresh_log_page()

    def on_mark_skip(self, event):
        if not self.mgmt_selected:
            wx.MessageBox("请先勾选课件", "提示", wx.ICON_INFORMATION)
            return
        for acc, cid in self.mgmt_selected:
            self.skip_cids.add(f"{acc}|{cid}")
            item = next((d for d in self.mgmt_data if d['acc'] == acc and d['cid'] == cid), None)
            if item:
                self.pack_log.append({"account": acc, "cid": cid, "name": item['name'],
                                      "pack_time": int(time.time() * 1000), "output_path": "", "skipped": True})
        self.settings["skip_cids"] = list(self.skip_cids)
        save_settings(self.settings)
        save_pack_log(self.pack_log)
        self.refresh_mgmt()
        self.refresh_log_page()

    def on_unmark_skip(self, event):
        if not self.mgmt_selected:
            wx.MessageBox("请先勾选课件", "提示", wx.ICON_INFORMATION)
            return
        for acc, cid in self.mgmt_selected:
            self.skip_cids.discard(f"{acc}|{cid}")
            self.pack_log = [rec for rec in self.pack_log if not (
                rec['account'] == acc and rec['cid'] == cid and rec.get('skipped')
            )]
            if hasattr(self, '_last_pack_state'):
                self._last_pack_state.pop(f"{acc}_{cid}", None)
            non_skip_recs = [rec for rec in self.pack_log if rec['account'] == acc and rec['cid'] == cid and not rec.get('skipped')]
            if non_skip_recs:
                latest_non_skip = max(non_skip_recs, key=lambda x: x['pack_time'])
                self.pack_log.remove(latest_non_skip)
        self.settings["skip_cids"] = list(self.skip_cids)
        save_settings(self.settings)
        save_pack_log(self.pack_log)
        self.refresh_mgmt()
        self.refresh_log_page()

    def on_delete_selected(self, event):
        if not self.mgmt_selected:
            wx.MessageBox("请先勾选课件", "提示", wx.ICON_INFORMATION)
            return
        dlg = wx.MessageDialog(self, "确定要删除勾选的课件吗？此操作不可恢复！", "危险操作",
                               wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
        if dlg.ShowModal() != wx.ID_YES:
            dlg.Destroy()
            return
        dlg.Destroy()
        for acc, cid in list(self.mgmt_selected):
            item = next((d for d in self.mgmt_data if d['acc'] == acc and d['cid'] == cid), None)
            if item:
                shutil.rmtree(item['dir'], ignore_errors=True)
                if acc in self.courseware_data and cid in self.courseware_data[acc]:
                    del self.courseware_data[acc][cid]
        self.refresh_mgmt()
        self.refresh_log_page()

    def on_mgmt_browse(self, event):
        current = self.mgmt_dir_text.GetValue().strip()
        default_path = current if os.path.isdir(current) else get_system_root()   # 修复拼写
        dlg = wx.DirDialog(self, "选择 Data 目录", defaultPath=default_path, style=wx.DD_DEFAULT_STYLE)
        if dlg.ShowModal() == wx.ID_OK:
            self.mgmt_dir_text.SetValue(dlg.GetPath())
            self.refresh_mgmt()
        dlg.Destroy()

    def on_pack_selected(self, event):
        if not self.mgmt_selected:
            wx.MessageBox("请勾选要打包的课件", "提示", wx.ICON_INFORMATION)
            return
        default_path = self.output_root if os.path.isdir(self.output_root) else get_system_root()
        dlg = wx.DirDialog(self, "选择保存目录", defaultPath=default_path, style=wx.DD_DEFAULT_STYLE)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        output_dir = dlg.GetPath()
        dlg.Destroy()
        threading.Thread(target=self._do_pack_multiple, args=(list(self.mgmt_selected), output_dir), daemon=True).start()

    def _do_pack_multiple(self, to_pack, output_dir):
        total = len(to_pack)
        failed = []
        enable_checksum = self.settings.get('enable_hash_check', True)
        for idx, (acc, cid) in enumerate(to_pack):
            if acc not in self.courseware_data or cid not in self.courseware_data[acc]:
                failed.append((acc, cid))
                continue
            info, files, cw_dir = self.courseware_data[acc][cid]
            invalid_files = validate_files(files, cw_dir, enable_checksum)
            if invalid_files:
                msg = f"课件“{info.get('Name')}”文件不完整，跳过打包：\n"
                msg += "\n".join([f"{f}: {r}" for f, r in invalid_files])
                wx.CallAfter(wx.MessageBox, msg, "校验失败", wx.ICON_ERROR)
                failed.append((acc, cid))
                continue
            json_name = f"{cid}.json"
            enbx_name = sanitize_filename(self._safe_str(info.get('Name')), cid) + '.enbx'
            out_path = os.path.join(self.account_paths.get(acc, output_dir), enbx_name)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            try:
                pack_courseware(cw_dir, json_name, out_path,
                                progress_callback=lambda v: isinstance(v, int) and wx.CallAfter(self.mgmt_gauge.SetValue, v))
                self.pack_log.append({
                    "account": acc, "cid": cid, "name": enbx_name,
                    "pack_time": int(time.time() * 1000), "output_path": out_path
                })
                save_pack_log(self.pack_log)
            except Exception as e:
                failed.append((acc, cid))
                wx.CallAfter(wx.MessageBox, f"打包失败 {enbx_name}: {e}", "错误", wx.ICON_ERROR)
            wx.CallAfter(self.mgmt_gauge.SetValue, int((idx + 1) / total * 100))
        if failed:
            wx.CallAfter(wx.MessageBox, f"以下课件打包失败: {failed}", "警告", wx.ICON_WARNING)
        wx.CallAfter(self.refresh_mgmt)
        wx.CallAfter(self.refresh_log_page)

    # ---------- 打包记录页面 ----------
    def init_log_page(self):
        sz = wx.BoxSizer(wx.VERTICAL)
        self.log_list = wx.ListCtrl(self.log_panel, style=wx.LC_REPORT)
        for col, width in [("账户", 120), ("课件名称", 250), ("打包时间/状态", 160), ("输出路径", 300)]:
            self.log_list.InsertColumn(self.log_list.GetColumnCount(), col, width=width)
        btn_sz = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in [("删除选中记录", self.on_delete_log),
                               ("去除重复记录", self.on_remove_duplicates),
                               ("去除过期记录", self.on_remove_expired)]:
            btn = wx.Button(self.log_panel, label=label)
            btn.Bind(wx.EVT_BUTTON, handler)
            btn_sz.Add(btn, 0, wx.ALL, 5)
        sz.Add(self.log_list, 1, wx.EXPAND | wx.ALL, 10)
        sz.Add(btn_sz, 0, wx.ALIGN_RIGHT | wx.ALL, 5)
        self.log_panel.SetSizer(sz)
        self.refresh_log_page()

    def refresh_log_page(self):
        """刷新打包记录列表，正确显示失败、跳过等状态"""
        self.log_list.DeleteAllItems()
        for rec in self.pack_log:
            row = self.log_list.InsertItem(self.log_list.GetItemCount(), rec['account'])
            self.log_list.SetItem(row, 1, rec.get('name', ''))
            if rec.get("failed"):
                self.log_list.SetItem(row, 2, f"打包失败: {rec.get('reason', '未知错误')}")
                self.log_list.SetItemTextColour(row, wx.RED)
            elif rec.get("skipped"):
                self.log_list.SetItem(row, 2, "已跳过/撤销")
                self.log_list.SetItemTextColour(row, wx.Colour(128, 128, 128))
            else:
                self.log_list.SetItem(row, 2, time.strftime("%Y-%m-%d %H:%M", time.localtime(rec['pack_time'] / 1000)))
            self.log_list.SetItem(row, 3, rec.get('output_path', ''))

    def on_remove_duplicates(self, event):
        if not self.pack_log:
            wx.MessageBox("记录为空", "提示", wx.ICON_INFORMATION)
            return
        unique = {}
        for rec in self.pack_log:
            key = (rec['account'], rec['cid'])
            if key not in unique or rec['pack_time'] > unique[key]['pack_time']:
                unique[key] = rec
        self.pack_log = sorted(unique.values(), key=lambda x: x['pack_time'], reverse=True)
        save_pack_log(self.pack_log)
        self.refresh_log_page()
        self.refresh_mgmt()

    def on_remove_expired(self, event):
        if not self.pack_log:
            wx.MessageBox("没有打包记录", "提示", wx.ICON_INFORMATION)
            return
        valid_keys = {(acc, cid) for acc, cw in self.courseware_data.items() for cid in cw}
        new_log = [rec for rec in self.pack_log if (rec['account'], rec['cid']) in valid_keys]
        removed = len(self.pack_log) - len(new_log)
        if removed == 0:
            wx.MessageBox("没有发现过期记录", "提示", wx.ICON_INFORMATION)
            return
        dlg = wx.MessageDialog(self, f"发现 {removed} 条过期记录，是否删除？", "确认", wx.YES_NO | wx.ICON_QUESTION)
        if dlg.ShowModal() != wx.ID_YES:
            dlg.Destroy()
            return
        dlg.Destroy()
        self.pack_log = new_log
        save_pack_log(self.pack_log)
        self.refresh_log_page()
        self.refresh_mgmt()

    def on_delete_log(self, event):
        selected = [self.log_list.GetFirstSelected()]
        while selected[-1] != -1:
            selected.append(self.log_list.GetNextSelected(selected[-1]))
        to_remove = [idx for idx in selected if idx != -1]
        for idx in sorted(to_remove, reverse=True):
            del self.pack_log[idx]
        save_pack_log(self.pack_log)
        self.refresh_log_page()
        self.refresh_mgmt()

    # ---------- 设置页面 ----------
    def init_settings_page(self):
        main_sz = wx.BoxSizer(wx.VERTICAL)
        # 自动转存
        auto_box = wx.StaticBox(self.settings_panel, label="自动转存")
        auto_sz = wx.StaticBoxSizer(auto_box, wx.VERTICAL)
        self.auto_pack_cb = wx.CheckBox(auto_box, label="启用自动转存（每10秒检测新修改的课件）")
        self.auto_pack_cb.SetValue(self.auto_pack_enabled)
        self.auto_pack_cb.Bind(wx.EVT_CHECKBOX, self.on_auto_pack_toggle)
        auto_sz.Add(self.auto_pack_cb, 0, wx.ALL, 5)
        main_sz.Add(auto_sz, 0, wx.EXPAND | wx.ALL, 5)

        # 路径设置
        path_box = wx.StaticBox(self.settings_panel, label="路径设置")
        path_sz = wx.StaticBoxSizer(path_box, wx.VERTICAL)
        # 监控目录行
        row1_sz = wx.BoxSizer(wx.HORIZONTAL)
        row1_sz.Add(wx.StaticText(path_box, label="监控目录（Data）:", size=(120, -1)), 0, wx.ALIGN_CENTER_VERTICAL)
        self.monitor_dir_text = wx.TextCtrl(path_box, value=self.monitor_dir)
        row1_sz.Add(self.monitor_dir_text, 1, wx.EXPAND | wx.LEFT, 5)
        btn_mon = SquareButton(path_box, label="...")
        btn_mon.Bind(wx.EVT_BUTTON, self.on_select_monitor_dir)
        row1_sz.Add(btn_mon, 0, wx.LEFT, 5)
        path_sz.Add(row1_sz, 0, wx.EXPAND | wx.ALL, 5)

        # 输出根目录行
        row2_sz = wx.BoxSizer(wx.HORIZONTAL)
        row2_sz.Add(wx.StaticText(path_box, label="输出根目录:", size=(120, -1)), 0, wx.ALIGN_CENTER_VERTICAL)
        self.output_root_text = wx.TextCtrl(path_box, value=self.output_root)
        row2_sz.Add(self.output_root_text, 1, wx.EXPAND | wx.LEFT, 5)
        btn_out = SquareButton(path_box, label="...")
        btn_out.Bind(wx.EVT_BUTTON, self.on_select_output_dir)
        row2_sz.Add(btn_out, 0, wx.LEFT, 5)
        path_sz.Add(row2_sz, 0, wx.EXPAND | wx.ALL, 5)

        self.by_account_cb = wx.CheckBox(path_box, label="按手机号分文件夹")
        self.by_account_cb.SetValue(self.by_account)
        path_sz.Add(self.by_account_cb, 0, wx.ALL, 5)
        main_sz.Add(path_sz, 0, wx.EXPAND | wx.ALL, 5)

        # 校验选项
        chk_box = wx.StaticBox(self.settings_panel, label="校验选项")
        chk_sz = wx.StaticBoxSizer(chk_box, wx.VERTICAL)
        self.hash_check_cb = wx.CheckBox(chk_box, label="启用文件哈希校验")
        self.hash_check_cb.SetValue(self.settings.get('enable_hash_check', True))
        chk_sz.Add(self.hash_check_cb, 0, wx.ALL, 5)
        main_sz.Add(chk_sz, 0, wx.EXPAND | wx.ALL, 5)

        # 按账户自定义输出路径
        cust_box = wx.StaticBox(self.settings_panel, label="按账户自定义输出路径")
        cust_sz = wx.StaticBoxSizer(cust_box, wx.VERTICAL)
        self.account_list = wx.ListCtrl(cust_box, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.account_list.InsertColumn(0, "手机号", 120)
        self.account_list.InsertColumn(1, "自定义路径", 300)
        cust_sz.Add(self.account_list, 1, wx.EXPAND | wx.ALL, 5)
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in [("刷新账户列表", self.on_refresh_account_list),
                                ("设置路径", self.on_set_account_path),
                                ("清除路径", self.on_clear_account_path)]:
            btn = wx.Button(cust_box, label=label)
            btn.Bind(wx.EVT_BUTTON, handler)
            btn_row.Add(btn, 0, wx.ALL, 5)
        cust_sz.Add(btn_row, 0, wx.ALIGN_LEFT | wx.ALL, 5)
        main_sz.Add(cust_sz, 1, wx.EXPAND | wx.ALL, 5)

        # 开机自启动服务
        service_box = wx.StaticBox(self.settings_panel, label="开机自启动服务")
        service_sz = wx.StaticBoxSizer(service_box, wx.VERTICAL)

        status_row = wx.BoxSizer(wx.HORIZONTAL)
        self.service_status_label = wx.StaticText(service_box, label="检测中...")
        status_row.Add(self.service_status_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        service_sz.Add(status_row, 0, wx.EXPAND | wx.ALL, 5)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_install = wx.Button(service_box, label="安装服务")
        self.btn_install.Bind(wx.EVT_BUTTON, lambda e: self.install_service())
        btn_row.Add(self.btn_install, 0, wx.ALL, 5)

        self.btn_uninstall = wx.Button(service_box, label="卸载服务")
        self.btn_uninstall.Bind(wx.EVT_BUTTON, lambda e: self.uninstall_service())
        btn_row.Add(self.btn_uninstall, 0, wx.ALL, 5)

        self.btn_refresh = wx.Button(service_box, label="刷新状态")
        self.btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self.refresh_service_status())
        btn_row.Add(self.btn_refresh, 0, wx.ALL, 5)
        service_sz.Add(btn_row, 0, wx.ALIGN_CENTER | wx.ALL, 5)

        main_sz.Add(service_sz, 0, wx.EXPAND | wx.ALL, 10)

        self.refresh_service_status()

        save_btn = wx.Button(self.settings_panel, label="保存设置")
        save_btn.Bind(wx.EVT_BUTTON, self.on_save_settings)
        main_sz.Add(save_btn, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        self.settings_panel.SetSizer(main_sz)
        self.refresh_account_list()

    def refresh_account_list(self):
        self.account_list.DeleteAllItems()
        for i, acc in enumerate(sorted(self.courseware_data.keys() | self.account_paths.keys())):
            self.account_list.InsertItem(i, acc)
            self.account_list.SetItem(i, 1, self.account_paths.get(acc, "") or "(默认)")

    def on_refresh_account_list(self, event):
        monitor_dir = self.monitor_dir_text.GetValue().strip()
        if os.path.isdir(monitor_dir):
            self.courseware_data.update(scan_data_dir(monitor_dir))
        self.refresh_account_list()

    def on_set_account_path(self, event):
        selected = self.account_list.GetFirstSelected()
        if selected == -1:
            wx.MessageBox("请先选择一个账户", "提示", wx.ICON_INFORMATION)
            return
        account = self.account_list.GetItemText(selected, 0)
        custom_path = self.account_paths.get(account, "")
        default_path = custom_path if os.path.isdir(custom_path) else get_system_root()
        dlg = wx.DirDialog(self, f"为 {account} 选择保存目录", defaultPath=default_path, style=wx.DD_DEFAULT_STYLE)
        if dlg.ShowModal() == wx.ID_OK:
            self.account_paths[account] = dlg.GetPath()
            self.account_list.SetItem(selected, 1, self.account_paths[account])
        dlg.Destroy()
    
    def on_clear_account_path(self, event):
        selected = self.account_list.GetFirstSelected()
        if selected == -1:
            wx.MessageBox("请先选择一个账户", "提示", wx.ICON_INFORMATION)
            return
        account = self.account_list.GetItemText(selected, 0)
        self.account_paths.pop(account, None)
        self.account_list.SetItem(selected, 1, "(默认)")

    def on_auto_pack_toggle(self, event):
        self.auto_pack_enabled = self.auto_pack_cb.GetValue()
        if self.auto_pack_enabled:
            self.monitor_timer.Start(10000)
        else:
            self.monitor_timer.Stop()

    def on_select_monitor_dir(self, event):
        current = self.monitor_dir_text.GetValue().strip()
        default_path = current if os.path.isdir(current) else get_system_root()
        dlg = wx.DirDialog(self, "选择监控目录", defaultPath=default_path, style=wx.DD_DEFAULT_STYLE)
        if dlg.ShowModal() == wx.ID_OK:
            self.monitor_dir_text.SetValue(dlg.GetPath())
        dlg.Destroy()

    def on_select_output_dir(self, event):
        current = self.output_root_text.GetValue().strip()
        default_path = current if os.path.isdir(current) else get_system_root()
        dlg = wx.DirDialog(self, "选择输出根目录", defaultPath=default_path, style=wx.DD_DEFAULT_STYLE)
        if dlg.ShowModal() == wx.ID_OK:
            self.output_root_text.SetValue(dlg.GetPath())
        dlg.Destroy()

    def on_save_settings(self, event):
        self.settings.update({
            'auto_pack': self.auto_pack_cb.GetValue(),
            'monitor_dir': self.monitor_dir_text.GetValue(),
            'output_root': self.output_root_text.GetValue(),
            'by_account': self.by_account_cb.GetValue(),
            'account_paths': self.account_paths,
            'skip_cids': list(self.skip_cids),
            'enable_hash_check': self.hash_check_cb.GetValue(),
            'fail_counts': self.fail_counts
        })
        save_settings(self.settings)
        self.monitor_dir = self.settings['monitor_dir']
        self.output_root = self.settings['output_root']
        self.by_account = self.settings['by_account']
        self.auto_pack_enabled = self.settings['auto_pack']
        if self.auto_pack_enabled:
            self.monitor_timer.Start(10000)
        else:
            self.monitor_timer.Stop()
        wx.MessageBox("设置已保存", "提示", wx.ICON_INFORMATION)

    # ---------- 服务安装相关 ----------
    def query_service_status(self):
        """查询服务安装状态，返回 True 表示已安装"""
        if not self.has_service_exe:
            return False
        try:
            result = subprocess.run(
                [self.auto_start_exe, '/query'],
                capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW
            )
            return result.returncode == 1
        except Exception:
            return False

    def _run_service_operation(self, operation, params):
        """后台执行服务操作（安装/卸载），完成后刷新状态"""
        try:
            if self.is_admin():
                subprocess.run(
                    [self.auto_start_exe, params],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW
                )
            else:
                self.run_as_admin(self.auto_start_exe, params, wait=True)
        except Exception as e:
            wx.CallAfter(wx.MessageBox, f"服务操作失败: {e}", "错误", wx.ICON_ERROR)
        finally:
            wx.CallAfter(self.update_service_status_ui)

    def install_service(self):
        threading.Thread(target=self._run_service_operation, args=("install", "/install"), daemon=True).start()

    def uninstall_service(self):
        threading.Thread(target=self._run_service_operation, args=("uninstall", "/uninstall"), daemon=True).start()

    def refresh_service_status(self):
        """刷新 UI 上的服务状态标签和按钮"""
        if not self.has_service_exe:
            self.service_status_label.SetLabel("未找到 tools\\AutoStartService.exe")
            self.service_status_label.SetForegroundColour(wx.RED)
            self.btn_install.Enable(False)
            self.btn_uninstall.Enable(False)
            return

        installed = self.query_service_status()
        if installed:
            text, color = "服务已安装", wx.GREEN
        else:
            text, color = "服务未安装", wx.RED

        self.service_status_label.SetLabel(text)
        self.service_status_label.SetForegroundColour(color)
        self.service_status_label.Refresh()
        self.service_status_label.Update()
        self.btn_install.Enable(not installed)
        self.btn_uninstall.Enable(installed)

    def update_service_status_ui(self):
        """供后台线程调用的 UI 更新"""
        self.refresh_service_status()

    # ---------- 自动监控与打包 ----------
    def on_monitor_tick(self, event):
        """定时器触发，检查新修改的课件并自动打包"""
        if not self.auto_pack_enabled:
            return
        if not hasattr(self, '_last_pack_state'):
            self._last_pack_state = {}
        new_data = scan_data_dir(self.monitor_dir)
        enable_checksum = self.settings.get('enable_hash_check', True)
        for acc, cw_dict in new_data.items():
            for cid, (info, files, cid_dir) in cw_dict.items():
                if f"{acc}|{cid}" in self.skip_cids:
                    continue
                key = f"{acc}_{cid}"
                json_mtime = info.get('UpdateTime', 0) / 1000.0
                latest = max((rec for rec in self.pack_log if rec['account'] == acc and rec['cid'] == cid),
                             key=lambda x: x['pack_time'], default=None)
                if latest is None or json_mtime > latest['pack_time'] / 1000.0:
                    if key not in self._last_pack_state or self._last_pack_state[key] < json_mtime:
                        safe_name = sanitize_filename(self._safe_str(info.get('Name')), cid) + '.enbx'
                        out_dir = self.account_paths.get(acc) or (os.path.join(self.output_root, acc) if self.by_account else self.output_root)
                        os.makedirs(out_dir, exist_ok=True)
                        out_path = os.path.join(out_dir, safe_name)
                        threading.Thread(target=self._auto_pack_thread,
                                         args=(cid_dir, f"{cid}.json", out_path, acc, cid, json_mtime, files, enable_checksum, info),daemon=True).start()

    def _on_auto_validation_fail(self, acc, cid, cw_name):
        """自动打包校验失败回调：累积失败次数，达到4次则永久跳过"""
        key = f"{acc}_{cid}"
        self.fail_counts[key] = self.fail_counts.get(key, 0) + 1
        if self.fail_counts[key] >= 4:
            self.skip_cids.add(f"{acc}|{cid}")
            del self.fail_counts[key]
            self.pack_log.append({
                "account": acc,
                "cid": cid,
                "name": cw_name,
                "pack_time": int(time.time() * 1000),
                "output_path": "",
                "skipped": True,
                "failed": True,
                "reason": "连续4次下载失败，自动跳过"
            })
            save_pack_log(self.pack_log)
        else:
            self.settings["fail_counts"] = self.fail_counts
            save_settings(self.settings)
            return
        self.settings["skip_cids"] = list(self.skip_cids)
        self.settings["fail_counts"] = self.fail_counts
        save_settings(self.settings)
        wx.CallAfter(self.refresh_mgmt)
        wx.CallAfter(self.refresh_log_page)

    def _on_auto_validation_success(self, acc, cid):
        key = f"{acc}_{cid}"
        if key in self.fail_counts:
            del self.fail_counts[key]
            self.settings["fail_counts"] = self.fail_counts
            save_settings(self.settings)

    def _update_last_pack_state(self, acc, cid, mtime):
        key = f"{acc}_{cid}"
        if not hasattr(self, '_last_pack_state'):
            self._last_pack_state = {}
        self._last_pack_state[key] = mtime

    def _auto_pack_thread(self, cw_dir, json_name, out_path, acc, cid, json_mtime, files, enable_checksum, info):
        """自动打包线程：校验 -> 打包 -> 记录 -> 显示撤销通知"""
        cw_name = self._safe_str(info.get('Name', '') if info else '')
        invalid = validate_files(files, cw_dir, enable_checksum)
        if invalid:
            wx.CallAfter(self._on_auto_validation_fail, acc, cid, cw_name)
            return

        wx.CallAfter(self._on_auto_validation_success, acc, cid)

        try:
            pack_courseware(cw_dir, json_name, out_path)
        except Exception as e:
            wx.CallAfter(self._add_failed_log, acc, cid, cw_name, out_path, str(e))
            return

        wx.CallAfter(self._update_last_pack_state, acc, cid, json_mtime)

        pack_record = {
            "account": acc, "cid": cid, "name": cw_name,
            "pack_time": int(time.time() * 1000), "output_path": out_path
        }
        self.pack_log.append(pack_record)
        save_pack_log(self.pack_log)
        wx.CallAfter(self.refresh_mgmt)
        wx.CallAfter(self.refresh_log_page)

        def undo_action():
            if os.path.exists(out_path):
                os.remove(out_path)
            try:
                self.pack_log.remove(pack_record)
            except ValueError:
                pass
            self.pack_log.append({
                "account": acc,
                "cid": cid,
                "name": cw_name,
                "pack_time": int(time.time() * 1000),
                "output_path": out_path,
                "skipped": True,
                "undo": True
            })
            self.skip_cids.add(f"{acc}|{cid}")
            self.settings["skip_cids"] = list(self.skip_cids)
            save_settings(self.settings)
            save_pack_log(self.pack_log)
            wx.CallAfter(self.refresh_mgmt)
            wx.CallAfter(self.refresh_log_page)

        dir_name = os.path.basename(os.path.dirname(out_path))
        wx.CallAfter(ToastNotification, self,
                     f"文件 {os.path.basename(out_path)} 已保存到 {dir_name}",
                     undo_action)

    def _add_failed_log(self, acc, cid, cw_name, out_path, error_msg):
        """添加自动打包失败的日志记录"""
        self.pack_log.append({
            "account": acc,
            "cid": cid,
            "name": cw_name,
            "pack_time": int(time.time() * 1000),
            "output_path": out_path,
            "failed": True,
            "reason": f"自动打包异常: {error_msg}"
        })
        save_pack_log(self.pack_log)
        wx.CallAfter(self.refresh_mgmt)
        wx.CallAfter(self.refresh_log_page)

    # ---------- 关于页面 ----------
    def init_about_page(self):
        panel = self.about_panel
        main_sz = wx.BoxSizer(wx.VERTICAL)
        title = wx.StaticText(panel, label=APP_NAME)
        title.SetFont(wx.Font(18, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        main_sz.Add(title, 0, wx.ALIGN_CENTER | wx.TOP, 20)
        version = wx.StaticText(panel, label=f"版本 {VERSION}")
        version.SetForegroundColour(wx.Colour(100, 100, 100))
        main_sz.Add(version, 0, wx.ALIGN_CENTER | wx.BOTTOM, 10)
        link = wx.adv.HyperlinkCtrl(panel, -1, "项目主页: https://github.com/howdy213/ENAnalyzer", "https://github.com/howdy213/ENAnalyzer")
        main_sz.Add(link, 0, wx.ALIGN_CENTER | wx.ALL, 5)
        license_text = wx.StaticText(panel, label="许可证: GPLv3")
        main_sz.Add(license_text, 0, wx.ALIGN_CENTER | wx.TOP, 5)
        desc = wx.StaticText(panel, label="帮助教师管理希沃白板课件，提供打包、自动转存、跳过管理、去重记录等功能。")
        desc.Wrap(400)
        main_sz.Add(desc, 0, wx.ALIGN_CENTER | wx.ALL, 10)
        features = ["📦 课件打包为 ENBX 格式", "📁 按账户自动转存到指定目录",
                    "⏭️ 支持永久跳过不需要的课件", "🧹 打包记录去重与撤销",
                    "📊 最近课件快速预览",
                    "🔍 文件完整性校验"]
        for text in features:
            main_sz.Add(wx.StaticText(panel, label=text), 0, wx.ALIGN_CENTER | wx.ALL, 2)
        main_sz.AddStretchSpacer()
        copyright_info = wx.StaticText(panel, label="© 2026 howdy213")
        copyright_info.SetForegroundColour(wx.Colour(150, 150, 150))
        main_sz.Add(copyright_info, 0, wx.ALIGN_CENTER | wx.BOTTOM, 15)
        panel.SetSizer(main_sz)

    # ---------- 窗口关闭与退出 ----------
    def on_close(self, event):
        """关闭窗口时隐藏到托盘，而不是退出程序"""
        self.Hide()

    def on_real_exit(self, event):
        """真正的退出操作"""
        self.monitor_timer.Stop()
        self.tray_icon.RemoveIcon()
        self.Destroy()
        wx.GetApp().ExitMainLoop()


if __name__ == '__main__':
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except:
            pass

    app = wx.App()
    single_checker = wx.SingleInstanceChecker(f"{APP_NAME}_SingleInstance")
    if single_checker.IsAnotherRunning():
        if SILENT_MODE:
            sys.exit(0)
        else:
            wx.MessageBox("程序已经在运行中，请勿重复启动。", APP_NAME, wx.OK | wx.ICON_WARNING)
            sys.exit(0)
        os._exit(0)

    main_frame = MainFrame()
    main_frame._single_instance_checker = single_checker
    app.MainLoop()
