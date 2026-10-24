# ENAnalyzer.pyw
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
import time
import threading
import subprocess
import ctypes
import re
import sys
import wx
import wx.adv

# 导入公共模块
from Common import (
    APP_NAME, VERSION, get_system_root, get_current_user_name,
    get_active_user_profile, base_dir, sanitize_filename,
    CONFIG_DIR, PACK_LOG_PATH, SETTINGS_PATH, DEFAULT_OUTPUT_DIR,
    DEFAULT_DATA_DIR, load_settings, save_settings
)
from FunctionTab import FunctionTabPanel
from PackQueue import (
    JOB_PENDING, JOB_RUNNING, JOB_SUCCESS, JOB_FAILED, JOB_CANCELLED,
    EVENT_QUEUED, EVENT_STARTED, EVENT_PROGRESS, EVENT_FINISHED, EVENT_IDLE,
    PackJob, PackQueue, scan_data_dir
)

SILENT_MODE = "-silent" in sys.argv

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

# ---- 自定义控件 ----

class SquareButton(wx.Button):
    """保证按钮始终为正方形"""
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
    """带撤销按钮的浮动通知"""
    def __init__(self, parent, message, on_undo, timeout=7, bottom_margin=70):
        super().__init__(parent, flags=wx.BORDER_SIMPLE)
        self.on_undo = on_undo
        self.timer = wx.Timer(self)
        self.timeout = timeout
        self.bottom_margin = bottom_margin

        panel = wx.Panel(self)
        sz = wx.BoxSizer(wx.HORIZONTAL)

        btn_undo = SquareButton(panel, label="↩")
        btn_undo.Bind(wx.EVT_BUTTON, self.on_undo_click)
        sz.Add(btn_undo, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)

        msg_text = wx.StaticText(panel, label=message, style=wx.ST_ELLIPSIZE_END)
        msg_text.Wrap(300)
        sz.Add(msg_text, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)

        btn_close = SquareButton(panel, label="✕")
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.Dismiss())
        sz.Add(btn_close, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)

        panel.SetSizer(sz)
        panel.Layout()

        txt_w = msg_text.GetBestSize().width
        btn_h = btn_undo.GetBestSize().height
        total_w = btn_h * 2 + txt_w + 30 + 60
        total_w = max(total_w, 300)
        screen_w, screen_h = wx.DisplaySize()
        total_w = min(total_w, screen_w - 40)
        total_h = max(msg_text.GetBestSize().height, btn_h) + 20
        self.SetSize((total_w, total_h))
        panel.SetSize((total_w, total_h))

        self.Bind(wx.EVT_TIMER, self.on_timer, self.timer)
        self.timer.Start(1000)

        x = (screen_w - total_w) // 2
        y = screen_h - total_h - self.bottom_margin
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
    """系统托盘图标"""
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

# ---- 主窗口 ----

class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title=APP_NAME, size=(1050, 750))
        self.settings = load_settings()
        self.pack_log = load_pack_log()
        self.courseware_data = {}
        self.fail_counts = self.settings.get("fail_counts", {})
        self.fail_lock = threading.Lock()

        self.auto_start_exe = os.path.join(base_dir(), "tools", "AutoStartService.exe")
        self.has_service_exe = os.path.isfile(self.auto_start_exe)

        icon_path = os.path.join(base_dir(), "asset", "ENAnalyzer.ico")
        if os.path.exists(icon_path):
            self.SetIcon(wx.Icon(icon_path, wx.BITMAP_TYPE_ICO))

        self.data_dir = DEFAULT_DATA_DIR
        self.auto_pack_enabled = self.settings.get("auto_pack", False)
        self.monitor_dir = self.settings.get("monitor_dir", self.data_dir)
        self.output_root = self.settings.get("output_root", DEFAULT_OUTPUT_DIR)
        self.by_account = self.settings.get("by_account", True)
        self.account_paths = self.settings.get("account_paths", {})
        self.skip_cids = set(self.settings.get("skip_cids", []))

        self.notebook = wx.Notebook(self)
        self.home_panel = wx.Panel(self.notebook)
        self.mgmt_panel = wx.Panel(self.notebook)
        self.queue_panel = wx.Panel(self.notebook)
        self.log_panel = wx.Panel(self.notebook)
        self.settings_panel = wx.Panel(self.notebook)
        self.about_panel = wx.Panel(self.notebook)

        # 新增功能设置 Tab
        self.function_panel = FunctionTabPanel(self.notebook)

        self.notebook.AddPage(self.home_panel, "首页")
        self.notebook.AddPage(self.mgmt_panel, "课件管理")
        self.notebook.AddPage(self.queue_panel, "打包队列")
        self.notebook.AddPage(self.log_panel, "打包记录")
        self.notebook.AddPage(self.settings_panel, "转存设置")
        self.notebook.AddPage(self.function_panel, "功能设置")   # 新页面
        self.notebook.AddPage(self.about_panel, "关于")

        self.init_home_page()
        self.init_mgmt_page()
        self.init_queue_page()
        self.init_log_page()
        self.init_settings_page()
        self.init_about_page()

        # 后台打包队列（打包逻辑与界面分离，任务串行执行）
        self._monitor_scan_running = False
        self._pack_batches = {}
        self._batch_seq = 0
        self.pack_queue = PackQueue(callback=self._on_pack_queue_event)

        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.tray_icon = TaskBarIcon(self)

        self.monitor_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_monitor_tick, self.monitor_timer)
        if self.auto_pack_enabled:
            self.monitor_timer.Start(10000)

        self.refresh_mgmt()
        self.Center()

        if not SILENT_MODE:
            self.Show()

    # ---- 管理员权限相关（保留原逻辑） ----
    @staticmethod
    def is_admin():
        try:
            return ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            return False

    def run_as_admin(self, exe_path, params, wait=True):
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

    # ---- 首页 ----
    def init_home_page(self):
        self.home_scroll = wx.ScrolledWindow(self.home_panel)
        self.home_scroll.SetScrollRate(5, 5)
        self.home_scroll.SetBackgroundColour(wx.Colour(245, 245, 245))
        self.home_panel.SetSizer(wx.BoxSizer(wx.VERTICAL))
        self.home_panel.GetSizer().Add(self.home_scroll, 1, wx.EXPAND)

    def refresh_home_page(self):
        self.home_scroll.DestroyChildren()
        items = []
        for acc, cw_dict in self.courseware_data.items():
            for cid, (info, _, cid_dir) in cw_dict.items():
                json_mtime = info.get('UpdateTime', 0) / 1000.0
                file_mtime = self._get_courseware_file_mtime(cid_dir)
                size = sum(os.path.getsize(os.path.join(cid_dir, f))
                           for f in os.listdir(cid_dir)
                           if os.path.isfile(os.path.join(cid_dir, f)))
                status, color = self.get_pack_status(acc, cid, json_mtime)
                items.append({
                    'acc': acc, 'cid': cid,
                    'name': self._safe_str(info.get('Name')),
                    'author': self._safe_str(info.get('Author')),
                    'mtime': json_mtime,
                    'file_mtime': file_mtime,
                    'size': size,
                    'status': status, 'color': color
                })
        items.sort(key=lambda x: x['file_mtime'], reverse=True)
        top6 = items[:6]

        client_width = self.home_scroll.GetClientSize().width
        if client_width <= 0:
            client_width = 800
        col_width = (client_width - 30) // 3
        if col_width < 200:
            col_width = 200

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
                card.SetMaxSize((col_width, -1))
                card_sz = wx.BoxSizer(wx.VERTICAL)

                title = wx.StaticText(card, label=item['name'])
                title.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
                title.Wrap(col_width - 20)
                card_sz.Add(title, 0, wx.ALL | wx.EXPAND, 8)

                grid = wx.FlexGridSizer(cols=2, vgap=4, hgap=15)
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
                self._bind_open_dblclick(card, item)
                grid_sz.Add(card, 0, wx.EXPAND | wx.ALL, 0)

            for _ in range(len(top6), 6):
                grid_sz.Add(wx.Panel(self.home_scroll), 0, wx.EXPAND)

            sz.Add(grid_sz, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.home_scroll.SetSizer(sz)
        self.home_scroll.Layout()
        self.home_scroll.SetVirtualSize(sz.GetMinSize())

    # ---- 双击打开课件 ----
    def _bind_open_dblclick(self, ctrl, item):
        """为首页卡片及其所有子控件绑定双击打开事件"""
        if not hasattr(self, '_cursor_hand'):
            self._cursor_hand = wx.Cursor(wx.CURSOR_HAND)
        ctrl.Bind(wx.EVT_LEFT_DCLICK, lambda e, it=item: self.on_home_card_dblclick(it))
        try:
            ctrl.SetCursor(self._cursor_hand)
            ctrl.SetToolTip("双击打开课件")
        except Exception:
            pass
        for child in ctrl.GetChildren():
            self._bind_open_dblclick(child, item)

    def on_home_card_dblclick(self, item):
        path = self._find_latest_output(item['acc'], item['cid'])
        if not path:
            wx.MessageBox(f"课件“{item['name']}”尚未打包，无法打开。\n请先在“课件管理”中打包该课件。",
                          "无法打开", wx.ICON_INFORMATION)
            return
        self._confirm_open_file(item['name'], path)

    def _find_latest_output(self, acc, cid):
        """查找该课件最近一次有效打包的输出路径"""
        recs = [rec for rec in self.pack_log
                if rec.get('account') == acc and rec.get('cid') == cid
                and rec.get('output_path') and not rec.get('failed') and not rec.get('skipped')]
        if not recs:
            return None
        return max(recs, key=lambda r: r.get('pack_time', 0)).get('output_path')

    def _confirm_open_file(self, name, path):
        if not path or not os.path.exists(path):
            wx.MessageBox(f"未找到可打开的打包文件：\n{path or '(空)'}", "无法打开", wx.ICON_WARNING)
            return
        dlg = wx.MessageDialog(self, f"是否打开课件“{name}”？\n\n{path}", "打开课件",
                               wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
        ok = dlg.ShowModal() == wx.ID_YES
        dlg.Destroy()
        if ok:
            self._open_file(path)

    def _open_file(self, path):
        try:
            os.startfile(path)
        except Exception as e:
            wx.MessageBox(f"打开失败: {e}", "错误", wx.ICON_ERROR)

    # ---- 打包队列 ----
    def init_queue_page(self):
        sz = wx.BoxSizer(wx.VERTICAL)
        top = wx.BoxSizer(wx.HORIZONTAL)
        self.queue_summary = wx.StaticText(self.queue_panel, label="队列空闲")
        top.Add(self.queue_summary, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        btn_clear = wx.Button(self.queue_panel, label="清空已完成任务")
        btn_clear.Bind(wx.EVT_BUTTON, self.on_clear_finished_queue)
        top.Add(btn_clear, 0, wx.ALL, 5)
        sz.Add(top, 0, wx.EXPAND | wx.ALL, 5)

        self.queue_list = wx.ListCtrl(self.queue_panel, style=wx.LC_REPORT)
        for col, width in [("状态", 80), ("课件名称", 220), ("账户", 110),
                           ("来源", 60), ("进度", 70), ("说明", 220), ("输出路径", 300)]:
            self.queue_list.InsertColumn(self.queue_list.GetColumnCount(), col, width=width)
        sz.Add(self.queue_list, 1, wx.EXPAND | wx.ALL, 10)
        self.queue_panel.SetSizer(sz)

        self.queue_jobs = []
        self.queue_row_map = {}

    def _on_pack_queue_event(self, event, job):
        """队列回调运行于后台线程，统一切回界面线程处理"""
        wx.CallAfter(self._handle_pack_queue_event, event, job)

    def _handle_pack_queue_event(self, event, job):
        if event in (EVENT_QUEUED, EVENT_STARTED):
            self._update_queue_row(job)
            self._update_queue_summary()
        elif event == EVENT_PROGRESS:
            self._update_queue_row(job)
            if job is self.pack_queue.current_job:
                self.mgmt_gauge.SetValue(max(0, min(100, job.progress)))
        elif event == EVENT_FINISHED:
            self._update_queue_row(job)
            self._update_queue_summary()
            self._update_mgmt_status_for_job(job)
            self._handle_pack_finished(job)
        elif event == EVENT_IDLE:
            self._update_queue_summary()
            self.mgmt_gauge.SetValue(0)
            self.refresh_log_page()
            self.refresh_mgmt()

    def _update_queue_row(self, job):
        row = self.queue_row_map.get(job)
        if row is None:
            row = self.queue_list.GetItemCount()
            self.queue_list.InsertItem(row, "")
            self.queue_row_map[job] = row
            self.queue_jobs.append(job)
        status_map = {
            JOB_PENDING: ("等待中", wx.Colour(128, 128, 128)),
            JOB_RUNNING: ("打包中", wx.Colour(0, 120, 215)),
            JOB_SUCCESS: ("完成", wx.GREEN),
            JOB_FAILED: ("失败", wx.RED),
            JOB_CANCELLED: ("已取消", wx.Colour(128, 128, 128)),
        }
        status_text, color = status_map.get(job.status, (job.status, wx.BLACK))
        self.queue_list.SetItem(row, 0, status_text)
        self.queue_list.SetItem(row, 1, job.name)
        self.queue_list.SetItem(row, 2, job.acc)
        self.queue_list.SetItem(row, 3, "手动" if job.source == "manual" else "自动")
        self.queue_list.SetItem(row, 4, f"{max(0, min(100, job.progress))}%")
        self.queue_list.SetItem(row, 5, job.message)
        self.queue_list.SetItem(row, 6, job.out_path)
        self.queue_list.SetItemTextColour(row, color)

    def _update_queue_summary(self):
        pending = self.pack_queue.pending_count()
        running = self.pack_queue.running_count()
        if running:
            label = f"正在打包 {running} 个，等待 {pending} 个"
        elif pending:
            label = f"等待打包 {pending} 个"
        else:
            label = "队列空闲"
        total = len(self.queue_jobs)
        if total:
            label += f"（本次共 {total} 个任务）"
        self.queue_summary.SetLabel(label)

    def on_clear_finished_queue(self, event):
        keep = [j for j in self.queue_jobs if j.status in (JOB_PENDING, JOB_RUNNING)]
        self.queue_list.DeleteAllItems()
        self.queue_jobs = []
        self.queue_row_map = {}
        for job in keep:
            self._update_queue_row(job)
        self._update_queue_summary()

    def _update_mgmt_status_for_job(self, job):
        """任务完成后仅刷新对应行状态，避免整表重新扫描"""
        for row, item in enumerate(self.mgmt_data):
            if item['acc'] == job.acc and item['cid'] == job.cid:
                item['status'] = self.get_pack_status(job.acc, job.cid, item['mtime'])
                status_text, color = item['status']
                self.mgmt_list.SetItem(row, 6, status_text)
                self.mgmt_list.SetItemTextColour(row, color)
                break

    def _handle_pack_finished(self, job):
        # 自动任务只要通过完整性校验，就应清除失败计数（与原逻辑一致）
        if job.source == "auto" and job.failure_kind != "validation":
            self._on_auto_validation_success(job.acc, job.cid)
        if job.status == JOB_SUCCESS:
            record = {
                "account": job.acc, "cid": job.cid, "name": job.log_name,
                "pack_time": int(time.time() * 1000), "output_path": job.out_path
            }
            self.pack_log.append(record)
            save_pack_log(self.pack_log)
            self.refresh_log_page()
            if job.source == "auto":
                self._update_last_pack_state(job.acc, job.cid, job.json_mtime)
                self._show_pack_toast(job, record)
            self._mark_batch_done(job, success=True)
            return
        if job.status == JOB_CANCELLED:
            self._mark_batch_done(job, success=False)
            return
        if job.failure_kind == "validation":
            if job.source == "auto":
                self._on_auto_validation_fail(job.acc, job.cid, job.name)
            else:
                details = "\n".join(f"{f}: {r}" for f, r in job.invalid_files)
                wx.MessageBox(f"课件“{job.name}”文件不完整，跳过打包：\n{details}", "校验失败", wx.ICON_ERROR)
        else:
            if job.source == "auto":
                self._add_failed_log(job.acc, job.cid, job.name, job.out_path, job.message)
            else:
                wx.MessageBox(f"打包失败 {os.path.basename(job.out_path)}: {job.message}", "错误", wx.ICON_ERROR)
        self._mark_batch_done(job, success=False)

    def _mark_batch_done(self, job, success):
        batch_id = job.batch_id
        if batch_id is None:
            return
        batch = self._pack_batches.get(batch_id)
        if not batch:
            return
        batch['done'] += 1
        if not success:
            batch['failed'].append(job.name)
        if batch['done'] >= batch['total']:
            self._pack_batches.pop(batch_id, None)
            if batch['failed']:
                wx.MessageBox("以下课件打包失败:\n" + "\n".join(batch['failed']), "警告", wx.ICON_WARNING)

    def _show_pack_toast(self, job, record):
        if not self.settings.get('show_toast', True):
            return
        out_path = job.out_path
        acc, cid, cw_name = job.acc, job.cid, job.name

        def undo_action():
            if os.path.exists(out_path):
                os.remove(out_path)
            try:
                self.pack_log.remove(record)
            except ValueError:
                pass
            self.pack_log.append({
                "account": acc, "cid": cid, "name": cw_name,
                "pack_time": int(time.time() * 1000), "output_path": out_path,
                "skipped": True, "undo": True
            })
            self.skip_cids.add(f"{acc}|{cid}")
            self.settings["skip_cids"] = list(self.skip_cids)
            save_settings(self.settings)
            save_pack_log(self.pack_log)
            wx.CallAfter(self.refresh_mgmt)
            wx.CallAfter(self.refresh_log_page)

        dir_name = os.path.basename(os.path.dirname(out_path))
        bottom_margin = self.settings.get('toast_bottom_margin', 70)
        ToastNotification(self, f"文件 {os.path.basename(out_path)} 已保存到 {dir_name}",
                          undo_action, bottom_margin=bottom_margin)

    # ---- 课件管理 ----
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
        self.mgmt_list.Bind(wx.EVT_LEFT_DCLICK, self.on_mgmt_dblclick)
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

    def on_mgmt_dblclick(self, event):
        """双击课件管理列表：确认后打开对应课件"""
        row, _ = self.mgmt_list.HitTest(event.GetPosition())
        if row < 0 or row >= len(self.mgmt_data):
            event.Skip()
            return
        if self._get_mgmt_clicked_col(event.GetX()) == 0:
            # 勾选列用于勾选，双击不触发打开
            event.Skip()
            return
        item = self.mgmt_data[row]
        path = self._find_latest_output(item['acc'], item['cid'])
        if not path:
            wx.MessageBox(f"课件“{item['name']}”尚未打包，无法打开。\n请先勾选并打包该课件。",
                          "无法打开", wx.ICON_INFORMATION)
            return
        self._confirm_open_file(item['name'], path)

    def _get_mgmt_clicked_col(self, x_pos):
        cur = 0
        for i in range(self.mgmt_list.GetColumnCount()):
            cur += self.mgmt_list.GetColumnWidth(i)
            if x_pos < cur:
                return i
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
        default_path = current if os.path.isdir(current) else get_system_root()
        dlg = wx.DirDialog(self, "选择 Data 目录", defaultPath=default_path)
        if dlg.ShowModal() == wx.ID_OK:
            self.mgmt_dir_text.SetValue(dlg.GetPath())
            self.refresh_mgmt()
        dlg.Destroy()

    def on_pack_selected(self, event):
        if not self.mgmt_selected:
            wx.MessageBox("请勾选要打包的课件", "提示", wx.ICON_INFORMATION)
            return
        default_path = self.output_root if os.path.isdir(self.output_root) else get_system_root()
        dlg = wx.DirDialog(self, "选择保存目录", defaultPath=default_path)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        output_dir = dlg.GetPath()
        dlg.Destroy()

        self._batch_seq += 1
        batch_id = self._batch_seq
        batch = {'total': 0, 'done': 0, 'failed': []}
        missing = []
        for acc, cid in list(self.mgmt_selected):
            job = self._make_pack_job(acc, cid, output_dir, source='manual')
            if job is None:
                item = next((d for d in self.mgmt_data if d['acc'] == acc and d['cid'] == cid), None)
                missing.append(item['name'] if item else cid)
                continue
            job.batch_id = batch_id
            if self.pack_queue.add_job(job):
                batch['total'] += 1
        if batch['total'] == 0:
            if missing:
                wx.MessageBox("以下课件已不存在，无法打包:\n" + "\n".join(missing), "提示", wx.ICON_WARNING)
            else:
                wx.MessageBox("所选课件已在打包队列中", "提示", wx.ICON_INFORMATION)
            return
        batch['failed'].extend(missing)
        self._pack_batches[batch_id] = batch

    def _make_pack_job(self, acc, cid, output_dir, source):
        """根据当前课件数据创建打包任务（不执行打包）"""
        if acc not in self.courseware_data or cid not in self.courseware_data[acc]:
            return None
        info, files, cw_dir = self.courseware_data[acc][cid]
        name = self._safe_str(info.get('Name'))
        enbx_name = sanitize_filename(name, cid) + '.enbx'
        out_path = os.path.join(self.account_paths.get(acc) or output_dir, enbx_name)
        return PackJob(
            acc=acc, cid=cid, name=name, cw_dir=cw_dir, json_name=f"{cid}.json",
            out_path=out_path, files=files,
            enable_checksum=self.settings.get('enable_hash_check', True),
            backup_config=self.settings.get('backup_config', False),
            backup_unused=self.settings.get('backup_unused', False),
            source=source, log_name=(enbx_name if source == 'manual' else name)
        )

    # ---- 打包记录 ----
    def init_log_page(self):
        sz = wx.BoxSizer(wx.VERTICAL)
        self.log_list = wx.ListCtrl(self.log_panel, style=wx.LC_REPORT)
        for col, width in [("账户", 120), ("课件名称", 250), ("打包时间/状态", 160), ("输出路径", 300)]:
            self.log_list.InsertColumn(self.log_list.GetColumnCount(), col, width=width)
        self.log_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_log_item_activated)
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

    def on_log_item_activated(self, event):
        """双击打包记录：确认后打开对应课件"""
        idx = event.GetIndex()
        if idx < 0 or idx >= len(self.pack_log):
            return
        rec = self.pack_log[idx]
        path = rec.get('output_path') or ''
        name = rec.get('name') or rec.get('cid') or '课件'
        if not path:
            wx.MessageBox("该记录没有可打开的文件（可能为跳过或失败记录）。", "无法打开", wx.ICON_INFORMATION)
            return
        self._confirm_open_file(name, path)

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

    # ---- 转存设置 ----
    def init_settings_page(self):
        main_sz = wx.BoxSizer(wx.VERTICAL)
        auto_box = wx.StaticBox(self.settings_panel, label="自动转存")
        auto_sz = wx.StaticBoxSizer(auto_box, wx.VERTICAL)
        self.auto_pack_cb = wx.CheckBox(auto_box, label="启用自动转存（每10秒检测新修改的课件）")
        self.auto_pack_cb.SetValue(self.auto_pack_enabled)
        self.auto_pack_cb.Bind(wx.EVT_CHECKBOX, self.on_auto_pack_toggle)
        auto_sz.Add(self.auto_pack_cb, 0, wx.ALL, 5)
        main_sz.Add(auto_sz, 0, wx.EXPAND | wx.ALL, 5)

        path_box = wx.StaticBox(self.settings_panel, label="路径设置")
        path_sz = wx.StaticBoxSizer(path_box, wx.VERTICAL)
        row1_sz = wx.BoxSizer(wx.HORIZONTAL)
        row1_sz.Add(wx.StaticText(path_box, label="监控目录（Data）:", size=(120, -1)), 0, wx.ALIGN_CENTER_VERTICAL)
        self.monitor_dir_text = wx.TextCtrl(path_box, value=self.monitor_dir)
        row1_sz.Add(self.monitor_dir_text, 1, wx.EXPAND | wx.LEFT, 5)
        btn_mon = SquareButton(path_box, label="...")
        btn_mon.Bind(wx.EVT_BUTTON, self.on_select_monitor_dir)
        row1_sz.Add(btn_mon, 0, wx.LEFT, 5)
        path_sz.Add(row1_sz, 0, wx.EXPAND | wx.ALL, 5)

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

        chk_box = wx.StaticBox(self.settings_panel, label="校验选项")
        chk_sz = wx.StaticBoxSizer(chk_box, wx.VERTICAL)
        self.hash_check_cb = wx.CheckBox(chk_box, label="启用文件哈希校验")
        self.hash_check_cb.SetValue(self.settings.get('enable_hash_check', True))
        chk_sz.Add(self.hash_check_cb, 0, wx.ALL, 5)
        main_sz.Add(chk_sz, 0, wx.EXPAND | wx.ALL, 5)

        backup_box = wx.StaticBox(self.settings_panel, label="备份与通知")
        backup_sz = wx.StaticBoxSizer(backup_box, wx.VERTICAL)
        self.show_toast_cb = wx.CheckBox(backup_box, label="显示浮窗通知")
        self.show_toast_cb.SetValue(self.settings.get('show_toast', True))
        backup_sz.Add(self.show_toast_cb, 0, wx.ALL, 5)

        self.backup_config_cb = wx.CheckBox(backup_box, label="保存课件配置 JSON 到 dist/ 目录")
        self.backup_config_cb.SetValue(self.settings.get('backup_config', False))
        backup_sz.Add(self.backup_config_cb, 0, wx.ALL, 5)

        self.backup_unused_cb = wx.CheckBox(backup_box, label="保存未使用的文件到 dist/ 目录（含配置 JSON）")
        self.backup_unused_cb.SetValue(self.settings.get('backup_unused', False))
        self.backup_unused_cb.Bind(wx.EVT_CHECKBOX, self.on_backup_unused_toggle)
        backup_sz.Add(self.backup_unused_cb, 0, wx.ALL, 5)

        toast_row = wx.BoxSizer(wx.HORIZONTAL)
        toast_row.Add(wx.StaticText(backup_box, label="通知距离底部高度 (px):"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5)
        self.toast_bottom_spin = wx.SpinCtrl(backup_box, value=str(self.settings.get('toast_bottom_margin', 70)),
                                             min=0, max=500, size=(80, -1))
        toast_row.Add(self.toast_bottom_spin, 0, wx.ALL, 5)
        preview_btn = wx.Button(backup_box, label="预览通知")
        preview_btn.Bind(wx.EVT_BUTTON, self.on_preview_toast)
        toast_row.Add(preview_btn, 0, wx.ALL, 5)
        backup_sz.Add(toast_row, 0, wx.EXPAND | wx.ALL, 5)

        self.on_backup_unused_toggle(None)
        main_sz.Add(backup_sz, 0, wx.EXPAND | wx.ALL, 5)

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

    def on_backup_unused_toggle(self, event):
        if self.backup_unused_cb.GetValue():
            self.backup_config_cb.SetValue(True)
            self.backup_config_cb.Enable(False)
        else:
            self.backup_config_cb.Enable(True)

    def on_preview_toast(self, event):
        try:
            bottom_margin = self.toast_bottom_spin.GetValue()
        except Exception:
            bottom_margin = 70
        ToastNotification(self, "这是一条测试通知，自动消失", lambda: None, timeout=5, bottom_margin=bottom_margin)

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
        dlg = wx.DirDialog(self, f"为 {account} 选择保存目录", defaultPath=default_path)
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
            self.monitor_timer.Start(8000)
        else:
            self.monitor_timer.Stop()

    def on_select_monitor_dir(self, event):
        current = self.monitor_dir_text.GetValue().strip()
        default_path = current if os.path.isdir(current) else get_system_root()
        dlg = wx.DirDialog(self, "选择监控目录", defaultPath=default_path)
        if dlg.ShowModal() == wx.ID_OK:
            self.monitor_dir_text.SetValue(dlg.GetPath())
        dlg.Destroy()

    def on_select_output_dir(self, event):
        current = self.output_root_text.GetValue().strip()
        default_path = current if os.path.isdir(current) else get_system_root()
        dlg = wx.DirDialog(self, "选择输出根目录", defaultPath=default_path)
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
            'fail_counts': self.fail_counts,
            'show_toast': self.show_toast_cb.GetValue(),
            'toast_bottom_margin': self.toast_bottom_spin.GetValue(),
            'backup_config': self.backup_config_cb.GetValue(),
            'backup_unused': self.backup_unused_cb.GetValue()
        })
        save_settings(self.settings)
        self.monitor_dir = self.settings['monitor_dir']
        self.output_root = self.settings['output_root']
        self.by_account = self.settings['by_account']
        self.auto_pack_enabled = self.settings['auto_pack']
        if self.auto_pack_enabled:
            self.monitor_timer.Start(8000)
        else:
            self.monitor_timer.Stop()
        wx.MessageBox("设置已保存", "提示", wx.ICON_INFORMATION)

    # ---- 服务安装（保留原逻辑） ----
    def query_service_status(self):
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
        try:
            if self.is_admin():
                subprocess.run([self.auto_start_exe, params], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
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
        self.refresh_service_status()

    # ---- 自动打包监控 ----
    def on_monitor_tick(self, event):
        if not self.auto_pack_enabled or self._monitor_scan_running:
            return
        if not hasattr(self, '_last_pack_state'):
            self._last_pack_state = {}
        self._monitor_scan_running = True
        # 目录扫描放到后台线程，避免定时任务卡住界面
        threading.Thread(
            target=self._monitor_scan_worker,
            args=(self.monitor_dir, set(self.skip_cids), list(self.pack_log)),
            daemon=True
        ).start()

    def _monitor_scan_worker(self, monitor_dir, skip_cids, pack_log):
        try:
            new_data = scan_data_dir(monitor_dir)
        except Exception:
            new_data = {}
        wx.CallAfter(self._process_monitor_scan, new_data, skip_cids, pack_log)

    def _process_monitor_scan(self, new_data, skip_cids, pack_log):
        """根据扫描结果将需要打包的课件加入后台队列"""
        self._monitor_scan_running = False
        if not self.auto_pack_enabled:
            return
        enable_checksum = self.settings.get('enable_hash_check', True)
        backup_config = self.settings.get('backup_config', False)
        backup_unused = self.settings.get('backup_unused', False)
        # 预先按课件汇总最新打包时间，避免逐个课件遍历整份历史记录
        latest_times = {}
        for rec in pack_log:
            key = (rec.get('account'), rec.get('cid'))
            t = rec.get('pack_time', 0)
            if t > latest_times.get(key, -1):
                latest_times[key] = t
        for acc, cw_dict in new_data.items():
            for cid, (info, files, cid_dir) in cw_dict.items():
                if f"{acc}|{cid}" in skip_cids:
                    continue
                key = f"{acc}_{cid}"
                json_mtime = info.get('UpdateTime', 0) / 1000.0
                latest_time = latest_times.get((acc, cid))
                if latest_time is not None and json_mtime <= latest_time / 1000.0:
                    continue
                if key in self._last_pack_state and self._last_pack_state[key] >= json_mtime:
                    continue
                cw_name = self._safe_str(info.get('Name'))
                safe_name = sanitize_filename(cw_name, cid) + '.enbx'
                out_dir = self.account_paths.get(acc) or \
                    (os.path.join(self.output_root, acc) if self.by_account else self.output_root)
                out_path = os.path.join(out_dir, safe_name)
                job = PackJob(
                    acc=acc, cid=cid, name=cw_name, cw_dir=cid_dir, json_name=f"{cid}.json",
                    out_path=out_path, files=files, enable_checksum=enable_checksum,
                    backup_config=backup_config, backup_unused=backup_unused,
                    source='auto', log_name=cw_name, json_mtime=json_mtime
                )
                self.pack_queue.add_job(job)

    def _on_auto_validation_fail(self, acc, cid, cw_name):
        key = f"{acc}_{cid}"
        self.fail_counts[key] = self.fail_counts.get(key, 0) + 1
        if self.fail_counts[key] >= 4:
            self.skip_cids.add(f"{acc}|{cid}")
            del self.fail_counts[key]
            self.pack_log.append({
                "account": acc, "cid": cid, "name": cw_name,
                "pack_time": int(time.time() * 1000), "output_path": "",
                "skipped": True, "failed": True, "reason": "连续4次下载失败，自动跳过"
            })
            save_pack_log(self.pack_log)
        else:
            self.settings["fail_counts"] = self.fail_counts
            save_settings(self.settings)
            return
        self.settings["skip_cids"] = list(self.skip_cids)
        self.settings["fail_counts"] = self.fail_counts
        save_settings(self.settings)

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

    def _add_failed_log(self, acc, cid, cw_name, out_path, error_msg):
        self.pack_log.append({
            "account": acc, "cid": cid, "name": cw_name,
            "pack_time": int(time.time() * 1000), "output_path": out_path,
            "failed": True, "reason": f"自动打包异常: {error_msg}"
        })
        save_pack_log(self.pack_log)
        wx.CallAfter(self.refresh_log_page)

    # ---- 关于页面 ----
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
                    "📊 最近课件快速预览", "🔍 文件完整性校验",
                    "💾 可选备份配置及未使用文件",
                    "🧰 Dependencies 清理与弹窗关闭"]
        for text in features:
            main_sz.Add(wx.StaticText(panel, label=text), 0, wx.ALIGN_CENTER | wx.ALL, 2)
        main_sz.AddStretchSpacer()
        copyright_info = wx.StaticText(panel, label="© 2026 howdy213")
        copyright_info.SetForegroundColour(wx.Colour(150, 150, 150))
        main_sz.Add(copyright_info, 0, wx.ALIGN_CENTER | wx.BOTTOM, 15)
        panel.SetSizer(main_sz)

    # ---- 窗口关闭 ----
    def on_close(self, event):
        self.Hide()

    def on_real_exit(self, event):
        self.monitor_timer.Stop()
        self.pack_queue.stop()
        self.tray_icon.RemoveIcon()
        self.Destroy()
        wx.GetApp().ExitMainLoop()

# ---- 程序入口 ----
if __name__ == '__main__':
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
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
