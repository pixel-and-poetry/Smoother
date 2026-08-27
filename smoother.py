# -*- coding: utf-8 -*-
"""
Smoother - macOS 菜单栏应用
使用 rumps 创建菜单栏应用，使用定时轮询监控 ~/Downloads 文件夹，
并根据规则自动把新文件移动并重命名到目标文件夹。

依赖安装:
    pip install rumps
"""

import os
import shutil
import threading
import time
from dataclasses import dataclass
from typing import List

import rumps


@dataclass
class Rule:
    """文件整理规则。

    attributes:
        extensions:    要匹配的扩展名列表（小写、不含点），如 ["jpg", "png"]
        destination:   目标文件夹路径（支持 ~ 展开）
        name_template:  重命名模板，支持 {date}（YYYY-MM-DD）与 {original}（原文件名不含扩展名）
    """

    extensions: List[str]
    destination: str
    name_template: str


class SmootherApp(rumps.App):
    """Smoother 菜单栏应用主类。"""

    def __init__(self):
        # 设置菜单栏显示的文本图标，name 为应用名称
        super().__init__(name="Smoother", title="🧹")
        # 监控目标目录：用户主目录下的 Downloads
        self.download_path = os.path.expanduser("~/Downloads")
        # 轮询线程句柄，初始为 None，点击“开始监控”时再创建
        self._thread = None
        # 用于通知轮询线程退出的事件
        self._stop_event = threading.Event()
        # 已知文件集合（保存绝对路径），用于对比发现新文件
        self._known_files = set()
        # 保证启停操作与 _known_files 访问的线程安全
        self._lock = threading.Lock()

        # 整理规则列表：扩展名命中即按对应规则移动 + 重命名
        self._rules: List[Rule] = [
            Rule(
                extensions=["jpg", "png", "pages", "pdf"],
                destination="~/Downloads/Smoother整理",
                name_template="{date}_{original}",
            )
        ]

        # 菜单项
        self.start_item = rumps.MenuItem("开始监控", callback=self.start_monitoring)
        self.stop_item = rumps.MenuItem("停止监控", callback=self.stop_monitoring)
        self.quit_item = rumps.MenuItem("退出", callback=self.quit_app)

        # 组装菜单（None 作为分隔符）
        self.menu = [self.start_item, self.stop_item, None, self.quit_item]

        # 初始状态：未监控时禁用“停止监控”
        self.stop_item.set_callback(None)

    # ------------------------------------------------------------------
    # 轮询逻辑
    # ------------------------------------------------------------------
    def _scan_once(self):
        """扫描一次下载目录，对比已知集合，发现新文件则整理。

        关键点：
        1. 用 os.scandir 获取目录条目，过滤掉目录，只保留文件。
        2. 与 self._known_files 对比，差集即为本次新出现的文件。
        3. 把新文件交给 _process_new_file 处理（匹配规则 → 移动 + 重命名）。
        4. 整段对比/更新在 self._lock 内执行，防止与 stop_monitoring 争用。
        """
        try:
            current_files = set()
            with os.scandir(self.download_path) as it:
                for entry in it:
                    if entry.is_file():
                        current_files.add(entry.path)
        except FileNotFoundError:
            # 目录被删除等异常情况，跳过本次扫描
            return

        with self._lock:
            # 已知集合为空表示首次扫描：直接建快照，不视为“新文件”
            first_scan = not self._known_files
            new_files = current_files - self._known_files
            self._known_files = current_files

        if first_scan:
            return

        for path in sorted(new_files):
            print(f"[Smoother] 检测到新文件: {path}", flush=True)
            self._process_new_file(path)

    def _process_new_file(self, src_path: str):
        """对新文件按规则匹配并移动 + 重命名。

        步骤：
        1. 取扩展名（小写、不含点）与原文件名（不含扩展名）。
        2. 在 self._rules 中找第一个 extensions 命中该扩展名的规则。
        3. 无匹配 → 打印“跳过（无匹配规则）”。
        4. 有匹配 → 展开目标目录（不存在则创建），按 name_template 生成新名；
           若模板未带扩展名，自动补原扩展名；目标重名则追加 _1/_2… 后缀。
        5. 用 shutil.move 移动（跨卷自动 copy+delete），成功后打印“已整理”。
        """
        # 文件可能在扫描与处理之间被删除
        if not os.path.isfile(src_path):
            return

        # 保留原始扩展名（含点、原大小写）用于补名；小写形式用于规则匹配
        root, ext_with_dot = os.path.splitext(src_path)
        ext = ext_with_dot.lower().lstrip(".")
        original = os.path.basename(root)

        # 查找第一条命中规则
        matched_rule: Rule = None
        for rule in self._rules:
            if ext in rule.extensions:
                matched_rule = rule
                break

        if matched_rule is None:
            print(
                f"[Smoother] 跳过（无匹配规则）: {os.path.basename(src_path)}",
                flush=True,
            )
            return

        # 准备目标目录
        dest_dir = os.path.expanduser(matched_rule.destination)
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as e:
            print(f"[Smoother] 创建目标目录失败: {dest_dir} ({e})", flush=True)
            return

        # 按模板生成新文件名
        date_str = time.strftime("%Y-%m-%d")
        new_name = matched_rule.name_template.format(date=date_str, original=original)
        # 模板若未显式包含扩展名，则补上原扩展名（保留大小写）
        if not os.path.splitext(new_name)[1]:
            new_name = new_name + ext_with_dot

        # 处理目标重名：追加 _1 / _2 ... 后缀
        dest_path = os.path.join(dest_dir, new_name)
        if os.path.exists(dest_path):
            name_part, ext_part = os.path.splitext(new_name)
            counter = 1
            while os.path.exists(dest_path):
                dest_path = os.path.join(dest_dir, f"{name_part}_{counter}{ext_part}")
                counter += 1

        # 移动（同卷为 rename，跨卷自动 copy+delete）
        try:
            shutil.move(src_path, dest_path)
            print(f"[Smoother] 已整理: {dest_path}", flush=True)
        except OSError as e:
            print(f"[Smoother] 移动失败: {src_path} ({e})", flush=True)

    def _poll_loop(self):
        """轮询线程主循环：每 3 秒扫描一次，直到收到停止信号。"""
        # 首次进入立即扫描一次，建立基准快照
        self._scan_once()
        while not self._stop_event.wait(timeout=3):
            self._scan_once()

    # ------------------------------------------------------------------
    # 菜单回调
    # ------------------------------------------------------------------
    def start_monitoring(self, _sender):
        """点击“开始监控”时启动轮询线程。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                rumps.notification("Smoother", "提示", "监控已在运行中")
                return

            # 确保目标目录存在
            if not os.path.isdir(self.download_path):
                rumps.notification(
                    "Smoother", "错误", f"目录不存在: {self.download_path}"
                )
                return

            # 重置停止信号与已知集合，启动后台轮询线程
            self._stop_event.clear()
            self._known_files = set()
            self._thread = threading.Thread(
                target=self._poll_loop, name="Smoother-Poll", daemon=True
            )
            self._thread.start()

            # 更新菜单可用状态
            self.start_item.set_callback(None)
            self.stop_item.set_callback(self.stop_monitoring)

            rumps.notification(
                "Smoother", "监控已启动", f"正在监控: {self.download_path}"
            )

    def stop_monitoring(self, _sender):
        """点击“停止监控”时停止轮询线程。"""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                rumps.notification("Smoother", "提示", "监控未在运行")
                return

            # 通知线程退出并等待其结束
            self._stop_event.set()
            self._thread.join(timeout=5)
            self._thread = None
            self._known_files = set()

            # 更新菜单可用状态
            self.start_item.set_callback(self.start_monitoring)
            self.stop_item.set_callback(None)

            rumps.notification("Smoother", "监控已停止", "已停止监控下载文件夹")

    def quit_app(self, _sender):
        """退出应用前清理轮询线程资源。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._stop_event.set()
                self._thread.join(timeout=5)
                self._thread = None
        rumps.quit_application()


if __name__ == "__main__":
    SmootherApp().run()
