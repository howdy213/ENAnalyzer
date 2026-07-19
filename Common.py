# Common.py
import os
import sys
import ctypes
import re
import json

APP_NAME = "ENAnalyzer"
VERSION = "1.0.0"

def get_system_root():
    """返回系统盘根目录，例如 C:\\"""
    drive = os.environ.get('SystemDrive', 'C:')
    return drive + '\\'

def get_current_user_name():
    """调用 Windows API GetUserNameW 获取当前登录用户名，失败回退到环境变量"""
    try:
        buf = ctypes.create_unicode_buffer(256)
        size = ctypes.c_ulong(len(buf))
        if ctypes.windll.advapi32.GetUserNameW(buf, ctypes.byref(size)):
            return buf.value
    except Exception:
        pass
    return os.environ.get('USERNAME', '')

def get_active_user_profile():
    """获取实际活动用户目录"""
    username = get_current_user_name()
    if username.upper() != 'SYSTEM':
        return os.path.join(get_system_root(), 'Users', username)
    else:
        sys.exit(0)

def base_dir():
    """获取应用程序根目录（兼容打包）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def sanitize_filename(name, default='untitled'):
    """清理文件名中的非法字符，限制长度"""
    if not name:
        name = default
    name = re.sub(r'[\\/*?:"<>|]', '_', name).strip(' .')
    return name[:200] if name else default

# 配置文件与日志路径
CONFIG_DIR = os.path.join(base_dir(), "config")
PACK_LOG_PATH = os.path.join(CONFIG_DIR, "pack_history.json")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
_user_profile = get_active_user_profile()
DEFAULT_OUTPUT_DIR = os.path.join(_user_profile, "Documents", "ENAnalyzer_Backup")
DEFAULT_DATA_DIR = os.path.join(_user_profile, 'AppData', 'Roaming', 'Seewo', 'EasiNote5', 'Data')

def load_settings():
    """加载程序设置，提供完整默认值"""
    defaults = {
        "auto_pack": False,
        "monitor_dir": DEFAULT_DATA_DIR,
        "output_root": DEFAULT_OUTPUT_DIR,
        "by_account": True,
        "account_paths": {},
        "skip_cids": [],
        "enable_hash_check": True,
        "fail_counts": {},
        "show_toast": True,
        "toast_bottom_margin": 70,
        "backup_config": False,
        "backup_unused": False
    }
    if not os.path.exists(SETTINGS_PATH):
        return defaults
    with open(SETTINGS_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    # 确保所有键都存在
    for key, val in defaults.items():
        data.setdefault(key, val)
    return data

def save_settings(settings):
    """保存设置到文件"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2)