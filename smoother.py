# -*- coding: utf-8 -*-
"""
Smoother - macOS 菜单栏应用
使用 rumps 创建菜单栏应用，使用定时轮询监控 ~/Downloads 文件夹，
并根据规则自动把新文件移动并重命名到目标文件夹。

依赖安装:
    pip install rumps
"""

import csv
import os
import shutil
import threading
import time
from dataclasses import dataclass
from typing import List

import rumps
from AppKit import (
    NSApp,
    NSImage,
    NSOKButton,
    NSOpenPanel,
    NSWorkspace,
)


@dataclass
class Rule:
    """文件整理规则。

    attributes:
        extensions:  要匹配的扩展名列表（小写、不含点），如 ["jpg", "png"]
        destination: 目标文件夹路径（支持 ~ 展开）

    注：命名模板已从规则移出，统一从 config.txt 读取（见 self._name_template）。
    """

    extensions: List[str]
    destination: str


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
        # 命名模板不再写在规则里，统一从 config.txt 读取（self._name_template）
        self._rules: List[Rule] = [
            Rule(
                extensions=["jpg", "png", "pages", "pdf"],
                destination="~/Downloads/Smoother整理",
            )
        ]

        # 命名模板配置文件：脚本同目录下的 config.txt
        # 启动时读取一次；文件不存在或为空则回退默认 {date}_{original}
        self._config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.txt"
        )
        self._name_template = self._load_name_template()

        # 菜单项
        self.start_item = rumps.MenuItem("开始监控", callback=self.start_monitoring)
        self.stop_item = rumps.MenuItem("停止监控", callback=self.stop_monitoring)
        self.sort_item = rumps.MenuItem(
            "整理到指定文件夹", callback=self.sort_to_folder
        )
        self.set_icon_item = rumps.MenuItem(
            "设置文件夹图标", callback=self.set_folder_icon
        )
        self.quit_item = rumps.MenuItem("退出", callback=self.quit_app)

        # 组装菜单（None 作为分隔符）
        # 结构：开始监控 / 停止监控  ||  整理到指定文件夹  ||  设置文件夹图标  ||  退出
        self.menu = [
            self.start_item,
            self.stop_item,
            None,
            self.sort_item,
            None,
            self.set_icon_item,
            None,
            self.quit_item,
        ]

        # 初始状态：未监控时禁用“停止监控”
        self.stop_item.set_callback(None)

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _load_name_template(self) -> str:
        """从脚本同目录的 config.txt 读取命名模板。

        支持占位符：
            {date}     当前日期 YYYY-MM-DD
            {original} 原文件名（不含扩展名）
            {counter}  重名时自动递增的序号
        文件不存在或为空时返回默认模板 {date}_{original}。
        """
        default = "{date}_{original}"
        try:
            if not os.path.isfile(self._config_path):
                return default
            with open(self._config_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            return content or default
        except OSError:
            return default

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

        # 按配置模板生成新文件名（支持 {date} / {original} / {counter}）
        date_str = time.strftime("%Y-%m-%d")
        template = self._name_template
        has_counter = "{counter}" in template

        counter = 1
        new_name = template.format(date=date_str, original=original, counter=counter)
        # 模板若未显式包含扩展名，则补上原扩展名（保留大小写）
        if not os.path.splitext(new_name)[1]:
            new_name = new_name + ext_with_dot
        dest_path = os.path.join(dest_dir, new_name)

        # 处理目标重名：
        # - 模板含 {counter}：递增 counter 并重新渲染，直到不重名
        # - 模板不含 {counter}：追加 _N 后缀（保留原行为）
        if os.path.exists(dest_path):
            if has_counter:
                while os.path.exists(dest_path):
                    counter += 1
                    new_name = template.format(
                        date=date_str, original=original, counter=counter
                    )
                    if not os.path.splitext(new_name)[1]:
                        new_name = new_name + ext_with_dot
                    dest_path = os.path.join(dest_dir, new_name)
            else:
                name_part, ext_part = os.path.splitext(new_name)
                while os.path.exists(dest_path):
                    dest_path = os.path.join(
                        dest_dir, f"{name_part}_{counter}{ext_part}"
                    )
                    counter += 1

        # 移动（同卷为 rename，跨卷自动 copy+delete）
        try:
            shutil.move(src_path, dest_path)
            print(f"[Smoother] 已整理: {dest_path}", flush=True)
            # 移动成功后追加一条整理记录到目标文件夹下的 整理日志.csv
            self._append_log(dest_dir, src_path, dest_path)
        except OSError as e:
            print(f"[Smoother] 移动失败: {src_path} ({e})", flush=True)

    def _append_log(self, dest_dir: str, src_path: str, dest_path: str):
        """把本次整理记录追加到目标文件夹下的 整理日志.csv。

        列：原文件名、新文件名、目标文件夹、整理时间（YYYY-MM-DD HH:MM:SS）。
        文件不存在（或为空）则自动创建并写入表头。
        使用标准库 csv，按 utf-8-sig 编码写入，便于 Excel 正确显示中文。
        """
        log_path = os.path.join(dest_dir, "整理日志.csv")
        # 文件不存在或为空 → 需要先写表头
        need_header = (not os.path.exists(log_path)) or os.path.getsize(log_path) == 0
        row = [
            os.path.basename(src_path),  # 原文件名（含扩展名）
            os.path.basename(dest_path),  # 新文件名（含扩展名）
            dest_dir,  # 目标文件夹
            time.strftime("%Y-%m-%d %H:%M:%S"),  # 整理时间
        ]
        try:
            with open(log_path, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                if need_header:
                    writer.writerow(["原文件名", "新文件名", "目标文件夹", "整理时间"])
                writer.writerow(row)
        except OSError as e:
            print(f"[Smoother] 写入日志失败: {log_path} ({e})", flush=True)

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

    # ------------------------------------------------------------------
    # 整理到指定文件夹（一次性批量移动）
    # ------------------------------------------------------------------
    def sort_to_folder(self, _sender):
        """点击“整理到指定文件夹”：先选时间范围，再选目标文件夹，把 ~/Downloads 根目录符合时间范围的文件移动过去。

        流程：
        1. rumps.alert 选择时间范围（最近24/48小时，取消则直接返回）。
        2. NSOpenPanel（仅文件夹）→ 选目标文件夹；取消则直接返回。
        3. 扫描 ~/Downloads 根目录所有文件（不含子文件夹）。
        4. 只处理修改时间在 threshold 内的文件；逐个移动，保留原文件名；
           目标重名追加 _1/_2…（保留扩展名）。
        5. 目标文件夹不存在自动创建。
        6. 每个文件移动成功后追加 整理日志.csv 一条记录（复用 _append_log）。
        7. 完成后发通知“整理完成”并打印汇总。
        """
        # 菜单栏应用默认是后台应用，先激活自身，否则面板可能不显示在前台
        NSApp.activateIgnoringOtherApps_(True)

        # 1) 选择要整理的文件时间范围
        click = rumps.alert(
            message="选择要整理的文件时间范围",
            title="Smoother",
            ok="最近24小时",
            other="最近48小时",
            cancel="取消",
        )
        # rumps.alert 实际返回按钮序号：
        #   ok（“最近24小时”）= 1，other（“最近48小时”）= -1，
        #   取消 / 关闭窗口 / 按 Esc 等返回其它值（如 0）。
        if click == 1:
            threshold = 24 * 3600
        elif click == -1:
            threshold = 48 * 3600
        else:
            return  # 取消或未选择

        # 2) 选择目标文件夹（只允许文件夹，不允许选文件）
        panel = NSOpenPanel.openPanel()
        panel.setTitle_("选择整理到的目标文件夹")
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setCanCreateDirectories_(True)
        if panel.runModal() != NSOKButton:
            return  # 用户取消
        dest_dir = panel.URLs()[0].path()

        # 目标文件夹不存在则创建
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as e:
            rumps.notification("Smoother", "错误", f"无法创建目标文件夹: {e}")
            return

        # 3) 扫描 ~/Downloads 根目录文件（仅文件，不含子文件夹）
        try:
            entries = []
            with os.scandir(self.download_path) as it:
                for entry in it:
                    if entry.is_file():
                        entries.append(entry.path)
        except FileNotFoundError:
            rumps.notification(
                "Smoother", "错误", f"下载目录不存在: {self.download_path}"
            )
            return

        # 4) 逐个移动，只处理修改时间在 threshold 内的文件；保留原文件名；目标重名追加 _1/_2…（保留扩展名）
        moved = 0
        now = time.time()
        for src_path in sorted(entries):
            # 源文件可能在循环过程中被改动/删除
            if not os.path.isfile(src_path):
                continue
            # 超出时间范围 → 跳过，不移动
            if now - os.path.getmtime(src_path) > threshold:
                continue
            original = os.path.basename(src_path)
            dest_path = os.path.join(dest_dir, original)
            if os.path.exists(dest_path):
                name_part, ext_part = os.path.splitext(original)
                counter = 1
                while os.path.exists(dest_path):
                    dest_path = os.path.join(
                        dest_dir, f"{name_part}_{counter}{ext_part}"
                    )
                    counter += 1
            try:
                shutil.move(src_path, dest_path)
                moved += 1
                # 5) 追加整理记录（CSV 不存在或为空时 _append_log 自动写表头）
                self._append_log(dest_dir, src_path, dest_path)
            except OSError as e:
                print(f"[Smoother] 移动失败: {src_path} ({e})", flush=True)

        # 6) 汇总
        print(f"[Smoother] 整理完成，共移动 {moved} 个文件", flush=True)
        rumps.notification(
            "Smoother", "整理完成", f"共移动 {moved} 个文件"
        )

    def quit_app(self, _sender):
        """退出应用前清理轮询线程资源。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._stop_event.set()
                self._thread.join(timeout=5)
                self._thread = None
        rumps.quit_application()

    # ------------------------------------------------------------------
    # 设置文件夹图标
    # ------------------------------------------------------------------
    def set_folder_icon(self, _sender):
        """点击“设置文件夹图标”：先选目标文件夹，再选图片，把图片设为文件夹图标。

        流程：
        1. NSOpenPanel（仅文件夹）→ 选目标文件夹。
        2. NSOpenPanel（仅 png/jpg/jpeg）→ 选图标图片。
        3. NSImage 加载图片，NSWorkspace.setIcon:forFile:options: 写入图标。
        成功发送“图标已应用”通知，失败打印错误。
        """
        # 菜单栏应用默认是后台应用，先激活自身，否则面板可能不显示在前台
        NSApp.activateIgnoringOtherApps_(True)

        # 1) 选择目标文件夹（只允许选择文件夹，不允许选文件）
        folder_panel = NSOpenPanel.openPanel()
        folder_panel.setTitle_("选择要设置图标的目标文件夹")
        folder_panel.setCanChooseFiles_(False)
        folder_panel.setCanChooseDirectories_(True)
        folder_panel.setAllowsMultipleSelection_(False)
        folder_panel.setCanCreateDirectories_(False)
        if folder_panel.runModal() != NSOKButton:
            return  # 用户取消
        folder_path = folder_panel.URLs()[0].path()

        # 2) 选择图片（限定 png / jpg / jpeg）
        image_panel = NSOpenPanel.openPanel()
        image_panel.setTitle_("选择作为图标的图片（png / jpg / jpeg）")
        image_panel.setCanChooseFiles_(True)
        image_panel.setCanChooseDirectories_(False)
        image_panel.setAllowsMultipleSelection_(False)
        image_panel.setAllowedFileTypes_(["png", "jpg", "jpeg"])
        if image_panel.runModal() != NSOKButton:
            return  # 用户取消
        image_path = image_panel.URLs()[0].path()

        # 3) 加载图片为 NSImage，再调用 NSWorkspace 写入文件夹图标
        image = NSImage.alloc().initWithContentsOfFile_(image_path)
        if image is None:
            print(
                f"[Smoother] 图标设置失败: 无法加载图片 {image_path}",
                flush=True,
            )
            rumps.notification("Smoother", "图标设置失败", "无法加载所选图片")
            return

        # setIcon:forFile:options:  返回 BOOL 表示是否设置成功；options 传 0 表示无附加选项
        ok = NSWorkspace.sharedWorkspace().setIcon_forFile_options_(
            image, folder_path, 0
        )
        if ok:
            rumps.notification(
                "Smoother",
                "图标已应用",
                f"已为 {os.path.basename(folder_path)} 设置图标",
            )
            print(f"[Smoother] 图标已应用: {folder_path}", flush=True)
        else:
            print(f"[Smoother] 图标设置失败: {folder_path}", flush=True)
            rumps.notification("Smoother", "图标设置失败", "设置图标时出错")


if __name__ == "__main__":
    SmootherApp().run()
