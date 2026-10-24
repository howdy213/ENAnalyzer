# PackQueue.py
# -*- coding: utf-8 -*-
"""课件打包核心逻辑与后台打包队列。

本模块不依赖 wxPython，可独立运行于后台线程；
界面层通过回调接收队列事件，避免打包过程阻塞界面。
"""

import os
import json
import time
import shutil
import zipfile
import hashlib
import threading
import queue as queue_mod
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

# 任务状态
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_SUCCESS = "success"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

# 队列事件
EVENT_QUEUED = "job_queued"
EVENT_STARTED = "job_started"
EVENT_PROGRESS = "job_progress"
EVENT_FINISHED = "job_finished"
EVENT_IDLE = "queue_idle"


def parse_courseware_json(json_path):
    """解析希沃课件 JSON 文件，返回 (课件信息字典, 文件列表)"""
    try:
        with open(json_path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
    except Exception:
        return None, []
    return data.get('Courseware', {}), data.get('CoursewareFiles', [])


def validate_files(files, courseware_dir, enable_checksum=True):
    """校验文件完整性"""
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
                    if hasher.hexdigest().lower() != expected_hash.lower():
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


def pack_courseware(courseware_dir, json_name, output_path, progress_callback=None,
                    backup_config=False, backup_unused=False):
    """将课件目录打包为 ENBX 文件。

    progress_callback 会在后台线程中被直接调用：
      - 传入 int 表示整体进度百分比（0-100）
      - 传入 str 表示提示信息
    """
    json_path = os.path.join(courseware_dir, json_name)
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"JSON 文件不存在: {json_path}")
    info, files = parse_courseware_json(json_path)
    if info is None:
        raise ValueError("JSON 解析失败")
    temp_dir = os.path.join(os.path.dirname(output_path), f"__temp_{time.time_ns()}")
    os.makedirs(temp_dir, exist_ok=True)
    # 复制文件
    for idx, f in enumerate(files):
        src = os.path.join(courseware_dir, f['FileName'])
        dst = os.path.join(temp_dir, f['RelativePath'].replace('/', os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            if progress_callback:
                progress_callback(f"⚠️ {f['FileName']} 复制失败: {e}")
        if progress_callback:
            progress_callback(int((idx + 1) / len(files) * 50))
    generate_config_xml(files, temp_dir)

    # 备份到 dist/
    do_config_backup = backup_config or backup_unused
    if do_config_backup:
        dist_dir = os.path.join(temp_dir, 'dist')
        os.makedirs(dist_dir, exist_ok=True)
        try:
            shutil.copy2(json_path, os.path.join(dist_dir, json_name))
        except Exception as e:
            if progress_callback:
                progress_callback(f"⚠️ 配置 JSON 备份失败: {e}")

    if backup_unused:
        used_names = {f['FileName'] for f in files}
        for item in os.listdir(courseware_dir):
            item_path = os.path.join(courseware_dir, item)
            if not os.path.isfile(item_path):
                continue
            if item not in used_names:
                try:
                    shutil.copy2(item_path, os.path.join(dist_dir, item))
                except Exception as e:
                    if progress_callback:
                        progress_callback(f"⚠️ 未使用文件备份失败 ({item}): {e}")

    # 打包为 ZIP
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _, files_in in os.walk(temp_dir):
            for fn in files_in:
                abs_path = os.path.join(root, fn)
                zf.write(abs_path, os.path.relpath(abs_path, temp_dir))
    shutil.rmtree(temp_dir, ignore_errors=True)
    if progress_callback:
        progress_callback(100)
    return output_path


def scan_data_dir(data_dir):
    """扫描希沃数据目录，返回结构：{ 账户ID: { 课件ID: (课件信息, 文件列表, 课件目录路径) } }"""
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


class PackJob:
    """单个打包任务"""

    def __init__(self, acc, cid, name, cw_dir, json_name, out_path, files=None,
                 enable_checksum=True, backup_config=False, backup_unused=False,
                 source="manual", log_name=None, json_mtime=0.0):
        self.acc = acc
        self.cid = cid
        self.name = name
        self.cw_dir = cw_dir
        self.json_name = json_name
        self.out_path = out_path
        self.files = files or []
        self.enable_checksum = enable_checksum
        self.backup_config = backup_config
        self.backup_unused = backup_unused
        self.source = source
        self.log_name = log_name if log_name is not None else name
        self.json_mtime = json_mtime

        self.status = JOB_PENDING
        self.progress = 0
        self.message = ""
        self.invalid_files = []
        self.failure_kind = ""      # validation / pack
        self.batch_id = None

        self.added_time = time.time()
        self.start_time = None
        self.end_time = None


class PackQueue:
    """串行后台打包队列。

    所有任务由唯一的后台线程依次处理，避免并发打包拖慢/卡死界面。
    callback(event, job) 会在后台线程中触发，界面层需自行切回主线程。
    """

    def __init__(self, callback=None):
        self._callback = callback
        self._jobs = []
        self._lock = threading.RLock()
        self._queue = queue_mod.Queue()
        self.current_job = None
        self._worker = threading.Thread(target=self._run, name="PackQueueWorker", daemon=True)
        self._worker.start()

    def add_job(self, job):
        """加入队列；若同一课件已在等待/打包中则忽略并返回 None"""
        with self._lock:
            for j in self._jobs:
                if j.status in (JOB_PENDING, JOB_RUNNING) and j.acc == job.acc and j.cid == job.cid:
                    return None
            self._jobs.append(job)
        self._queue.put(job)
        self._emit(EVENT_QUEUED, job)
        return job

    def jobs(self):
        with self._lock:
            return list(self._jobs)

    def pending_count(self):
        with self._lock:
            return sum(1 for j in self._jobs if j.status == JOB_PENDING)

    def running_count(self):
        with self._lock:
            return sum(1 for j in self._jobs if j.status == JOB_RUNNING)

    def stop(self):
        """请求后台线程在已入队任务处理完后退出"""
        self._queue.put(None)

    def _emit(self, event, job):
        cb = self._callback
        if cb is None:
            return
        try:
            cb(event, job)
        except Exception:
            pass

    def _notify_idle(self):
        if self._queue.empty():
            self._emit(EVENT_IDLE, None)

    def _discard(self, job):
        """任务结束后从内部活动列表移除，避免长时间运行内存累积"""
        with self._lock:
            if job in self._jobs:
                self._jobs.remove(job)

    def _run(self):
        while True:
            job = self._queue.get()
            if job is None:
                break
            if job.status == JOB_CANCELLED:
                job.end_time = time.time()
                self._discard(job)
                self._emit(EVENT_FINISHED, job)
                self._notify_idle()
                continue

            self.current_job = job
            job.status = JOB_RUNNING
            job.start_time = time.time()
            job.message = ""
            self._emit(EVENT_STARTED, job)
            try:
                invalid = validate_files(job.files, job.cw_dir, job.enable_checksum)
                if invalid:
                    job.failure_kind = "validation"
                    job.invalid_files = invalid
                    job.message = f"校验失败：{len(invalid)} 个文件不完整"
                    raise ValueError("文件完整性校验失败")

                os.makedirs(os.path.dirname(job.out_path), exist_ok=True)

                def progress(value):
                    if isinstance(value, int):
                        if value != job.progress:
                            job.progress = value
                            self._emit(EVENT_PROGRESS, job)
                    else:
                        job.message = str(value)
                        self._emit(EVENT_PROGRESS, job)

                pack_courseware(job.cw_dir, job.json_name, job.out_path,
                                progress_callback=progress,
                                backup_config=job.backup_config,
                                backup_unused=job.backup_unused)
                job.status = JOB_SUCCESS
                job.progress = 100
                job.message = ""
            except Exception as e:
                if job.failure_kind != "validation":
                    job.failure_kind = "pack"
                    job.message = str(e)
                job.status = JOB_FAILED
            finally:
                job.end_time = time.time()
                self.current_job = None
                self._discard(job)
                self._emit(EVENT_FINISHED, job)
                self._notify_idle()
