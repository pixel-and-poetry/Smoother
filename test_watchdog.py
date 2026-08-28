import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class H(FileSystemEventHandler):
    def on_created(self, event):
        print("CREATED:", event.src_path)
    def on_any_event(self, event):
        print("ANY EVENT:", event.event_type, event.src_path)

path = "/Users/banban/Downloads"
observer = Observer()
observer.schedule(H(), path, recursive=False)
observer.start()
print(">>> 开始监控", path)
print(">>> 请在下载文件夹里新建或拖入一个文件...")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    observer.stop()
observer.join()
print(">>> 监控结束")
