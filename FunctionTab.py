# FunctionTab.py
import os
import json
import shutil
import threading
import wx
import ctypes
from ctypes import wintypes

from Common import CONFIG_DIR, get_active_user_profile

# 独立的配置文件路径
FUNCTION_SETTINGS_PATH = os.path.join(CONFIG_DIR, "function_settings.json")

# 内置可快捷添加的窗口标题
BUILTIN_WINDOW_TITLES = [
    "Piano"
]

def load_function_settings():
    """加载功能设置，返回字典，含默认值"""
    defaults = {
        "dependency_monitor_list": [],
        "auto_close_titles": [],
        "close_enabled": False,
        "description_map": {
            "Geography_Textures": "星球"
        }
    }
    if not os.path.exists(FUNCTION_SETTINGS_PATH):
        return defaults
    try:
        with open(FUNCTION_SETTINGS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for key, val in defaults.items():
            data.setdefault(key, val)
        return data
    except Exception:
        return defaults

def save_function_settings(settings):
    """保存功能设置到文件"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(FUNCTION_SETTINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2)


class FunctionTabPanel(wx.Panel):
    """希沃白板功能设置 Tab 页（独立配置文件，手动保存）"""
    def __init__(self, parent):
        super().__init__(parent)
        self.func_settings = load_function_settings()

        self.dep_dir = os.path.join(get_active_user_profile(), 'AppData', 'Roaming', 'Seewo', 'EasiNote5', 'Dependencies')
        self.monitor_list = list(self.func_settings.get('dependency_monitor_list', []))
        self.descriptions = self.func_settings.get('description_map', {})

        self.auto_close_titles = list(self.func_settings.get('auto_close_titles', []))
        self.close_enabled = self.func_settings.get('close_enabled', False)

        self.init_ui()

        self.scan_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_timer, self.scan_timer)
        self.scan_timer.Start(2000)

        self.refresh_dep_list()
        self.refresh_title_list()

    def init_ui(self):
        main_sz = wx.BoxSizer(wx.VERTICAL)

        # ---------- 依赖管理区域 ----------
        dep_box = wx.StaticBox(self, label="Dependencies 目录管理")
        dep_sz = wx.StaticBoxSizer(dep_box, wx.VERTICAL)

        self.dep_info = wx.StaticText(dep_box, label=f"当前目录: {self.dep_dir}")
        dep_sz.Add(self.dep_info, 0, wx.ALL, 5)

        self.dep_list = wx.ListCtrl(dep_box, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.dep_list.InsertColumn(0, "目录名称", width=180)
        self.dep_list.InsertColumn(1, "描述", width=120)
        self.dep_list.InsertColumn(2, "监视", width=60)
        dep_sz.Add(self.dep_list, 1, wx.EXPAND | wx.ALL, 5)

        btn_sz = wx.BoxSizer(wx.HORIZONTAL)
        btn_refresh = wx.Button(dep_box, label="刷新列表")
        btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self.refresh_dep_list())
        btn_sz.Add(btn_refresh, 0, wx.ALL, 5)

        btn_delete = wx.Button(dep_box, label="删除选中")
        btn_delete.Bind(wx.EVT_BUTTON, self.on_delete_dep)
        btn_sz.Add(btn_delete, 0, wx.ALL, 5)

        btn_add_monitor = wx.Button(dep_box, label="添加监视")
        btn_add_monitor.Bind(wx.EVT_BUTTON, self.on_add_monitor)
        btn_sz.Add(btn_add_monitor, 0, wx.ALL, 5)

        btn_remove_monitor = wx.Button(dep_box, label="取消监视")
        btn_remove_monitor.Bind(wx.EVT_BUTTON, self.on_remove_monitor)
        btn_sz.Add(btn_remove_monitor, 0, wx.ALL, 5)

        dep_sz.Add(btn_sz, 0, wx.ALIGN_LEFT | wx.ALL, 5)
        main_sz.Add(dep_sz, 1, wx.EXPAND | wx.ALL, 10)

        # ---------- 窗口关闭管理区域 ----------
        win_box = wx.StaticBox(self, label="自动关闭 EasiNote 弹窗")
        win_sz = wx.StaticBoxSizer(win_box, wx.VERTICAL)

        self.enable_cb = wx.CheckBox(win_box, label="启用自动关闭")
        self.enable_cb.SetValue(self.close_enabled)
        self.enable_cb.Bind(wx.EVT_CHECKBOX, self.on_toggle_close)
        win_sz.Add(self.enable_cb, 0, wx.ALL, 5)

        self.title_list = wx.ListCtrl(win_box, style=wx.LC_REPORT)
        self.title_list.InsertColumn(0, "窗口标题", width=300)
        self.title_list.InsertColumn(1, "来源", width=100)
        win_sz.Add(self.title_list, 1, wx.EXPAND | wx.ALL, 5)

        add_sz = wx.BoxSizer(wx.HORIZONTAL)
        self.new_title_tc = wx.TextCtrl(win_box, size=(200, -1))
        add_sz.Add(self.new_title_tc, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        btn_add_title = wx.Button(win_box, label="添加标题")
        btn_add_title.Bind(wx.EVT_BUTTON, self.on_add_title)
        add_sz.Add(btn_add_title, 0, wx.ALL, 5)

        for title in BUILTIN_WINDOW_TITLES:
            btn = wx.Button(win_box, label=f"+ {title}", size=(100, 25))
            btn.Bind(wx.EVT_BUTTON, lambda e, t=title: self.add_quick_title(t))
            add_sz.Add(btn, 0, wx.ALL, 5)
        win_sz.Add(add_sz, 0, wx.EXPAND | wx.ALL, 5)

        btn_del_title = wx.Button(win_box, label="删除选中标题")
        btn_del_title.Bind(wx.EVT_BUTTON, self.on_delete_title)
        win_sz.Add(btn_del_title, 0, wx.ALIGN_RIGHT | wx.ALL, 5)

        main_sz.Add(win_sz, 1, wx.EXPAND | wx.ALL, 10)

        # ---------- 保存按钮 ----------
        btn_save = wx.Button(self, label="保存设置")
        btn_save.Bind(wx.EVT_BUTTON, self.on_save_settings)
        main_sz.Add(btn_save, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        self.SetSizer(main_sz)

    # ==================== 依赖目录管理 ====================
    def refresh_dep_list(self):
        self.dep_list.DeleteAllItems()
        if not os.path.isdir(self.dep_dir):
            self.dep_info.SetLabel(f"目录不存在: {self.dep_dir}")
            return
        self.dep_info.SetLabel(f"当前目录: {self.dep_dir}")
        try:
            items = os.listdir(self.dep_dir)
        except Exception:
            items = []
        idx = 0
        for name in items:
            full_path = os.path.join(self.dep_dir, name)
            if not os.path.isdir(full_path):
                continue
            desc = self.descriptions.get(name, "")
            monitored = "是" if name in self.monitor_list else ""
            self.dep_list.InsertItem(idx, name)
            self.dep_list.SetItem(idx, 1, desc)
            self.dep_list.SetItem(idx, 2, monitored)
            idx += 1

    def on_delete_dep(self, event):
        sel = self.dep_list.GetFirstSelected()
        if sel == -1:
            wx.MessageBox("请先选择一个目录", "提示", wx.ICON_INFORMATION)
            return
        name = self.dep_list.GetItemText(sel, 0)
        full_path = os.path.join(self.dep_dir, name)
        dlg = wx.MessageDialog(self, f"确定要删除目录 '{name}' 吗？", "确认删除",
                               wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
        if dlg.ShowModal() != wx.ID_YES:
            dlg.Destroy()
            return
        dlg.Destroy()
        try:
            shutil.rmtree(full_path, ignore_errors=True)
        except Exception as e:
            wx.MessageBox(f"删除失败: {e}", "错误", wx.ICON_ERROR)
        self.refresh_dep_list()

    def on_add_monitor(self, event):
        sel = self.dep_list.GetFirstSelected()
        if sel == -1:
            wx.MessageBox("请先选择一个目录", "提示", wx.ICON_INFORMATION)
            return
        name = self.dep_list.GetItemText(sel, 0)
        if name not in self.monitor_list:
            self.monitor_list.append(name)
            self.refresh_dep_list()

    def on_remove_monitor(self, event):
        sel = self.dep_list.GetFirstSelected()
        if sel == -1:
            wx.MessageBox("请先选择一个目录", "提示", wx.ICON_INFORMATION)
            return
        name = self.dep_list.GetItemText(sel, 0)
        if name in self.monitor_list:
            self.monitor_list.remove(name)
            self.refresh_dep_list()

    def check_monitored_dirs(self):
        if not os.path.isdir(self.dep_dir) or not self.monitor_list:
            return
        try:
            for item in os.listdir(self.dep_dir):
                if item in self.monitor_list:
                    full_path = os.path.join(self.dep_dir, item)
                    if os.path.isdir(full_path):
                        try:
                            shutil.rmtree(full_path, ignore_errors=False)
                        except Exception:
                            pass
        except Exception:
            pass

    # ==================== 窗口关闭管理 ====================
    def refresh_title_list(self):
        self.title_list.DeleteAllItems()
        for i, title in enumerate(self.auto_close_titles):
            source = "内置" if title in BUILTIN_WINDOW_TITLES else "自定义"
            self.title_list.InsertItem(i, title)
            self.title_list.SetItem(i, 1, source)

    def on_add_title(self, event):
        title = self.new_title_tc.GetValue().strip()
        if not title:
            return
        if title not in self.auto_close_titles:
            self.auto_close_titles.append(title)
            self.refresh_title_list()
            self.new_title_tc.SetValue("")

    def add_quick_title(self, title):
        if title not in self.auto_close_titles:
            self.auto_close_titles.append(title)
            self.refresh_title_list()

    def on_delete_title(self, event):
        sel = self.title_list.GetFirstSelected()
        if sel == -1:
            wx.MessageBox("请先选择一个标题", "提示", wx.ICON_INFORMATION)
            return
        title = self.title_list.GetItemText(sel, 0)
        self.auto_close_titles.remove(title)
        self.refresh_title_list()

    def on_toggle_close(self, event):
        self.close_enabled = self.enable_cb.GetValue()

    # ==================== 进程查找（获取所有EasiNote.exe的PID） ====================
    def _get_all_pids_by_name(self, process_name):
        """返回所有匹配进程名的PID列表"""
        TH32CS_SNAPPROCESS = 0x00000002
        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260)
            ]
        snapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == -1:
            return []
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        pids = []
        if ctypes.windll.kernel32.Process32First(snapshot, ctypes.byref(entry)):
            while True:
                exe_name = entry.szExeFile.decode('gbk', errors='ignore').lower()
                if exe_name == process_name.lower():
                    pids.append(entry.th32ProcessID)
                if not ctypes.windll.kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
        ctypes.windll.kernel32.CloseHandle(snapshot)
        return pids

    # ==================== 窗口关闭逻辑（支持标题和类名匹配，多进程） ====================
    def close_easinote_sub_windows(self):
        if not self.close_enabled or not self.auto_close_titles:
            return

        pids = self._get_all_pids_by_name("EasiNote.exe")
        if not pids:
            return

        # 目标字符串（小写，用于标题匹配，以及类名中可能包含的片段）
        target_titles_lower = {t.lower() for t in self.auto_close_titles}

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def get_window_text(hwnd):
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return ""
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value.strip()

        def get_window_class(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(hwnd, buf, 255)
            return buf.value.strip()

        def is_target(hwnd):
            """检查窗口是否匹配目标（标题或类名）"""
            title = get_window_text(hwnd).lower()
            if title and title in target_titles_lower:
                return True
            # 如果标题为空，检查类名是否包含目标字符串
            if not title:
                cls = get_window_class(hwnd).lower()
                for t in target_titles_lower:
                    if t in cls:
                        return True
            return False

        def close_window(hwnd):
            if is_target(hwnd):
                ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)

        def enum_child_recursive(hwnd_parent):
            def child_proc(hwnd, lparam):
                close_window(hwnd)
                # 递归枚举子窗口的子窗口
                ctypes.windll.user32.EnumChildWindows(hwnd, EnumChildProc(child_proc), 0)
                return True
            ctypes.windll.user32.EnumChildWindows(hwnd_parent, EnumChildProc(child_proc), 0)

        # 枚举所有顶层窗口，属于任一EasiNote进程的则检查并递归
        def top_proc(hwnd, lparam):
            process_id = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if process_id.value in pids:
                close_window(hwnd)
                enum_child_recursive(hwnd)
            return True

        ctypes.windll.user32.EnumWindows(EnumWindowsProc(top_proc), 0)

    # ==================== 定时器 ====================
    def on_timer(self, event):
        self.check_monitored_dirs()
        threading.Thread(target=self.close_easinote_sub_windows, daemon=True).start()

    # ==================== 保存设置 ====================
    def on_save_settings(self, event):
        self.func_settings['dependency_monitor_list'] = self.monitor_list
        self.func_settings['auto_close_titles'] = self.auto_close_titles
        self.func_settings['close_enabled'] = self.close_enabled
        save_function_settings(self.func_settings)
        wx.MessageBox("功能设置已保存", "提示", wx.ICON_INFORMATION)
