import logging
import queue
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from paoding.ingestion.normalizer import SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)


def watch_drop(runtime):
    root = runtime.settings.drop_dir.resolve()
    roots = [root]
    if runtime.settings.workbuddy_dir:
        roots.append(runtime.settings.workbuddy_dir.resolve())
    for directory in roots:
        directory.mkdir(parents=True, exist_ok=True)
    pending = queue.Queue()

    class Handler(FileSystemEventHandler):
        def on_created(self, event):
            if not event.is_directory:
                pending.put(Path(event.src_path))

        def on_modified(self, event):
            if not event.is_directory:
                pending.put(Path(event.src_path))

        def on_moved(self, event):
            if not event.is_directory:
                pending.put(Path(event.dest_path))

    observer = Observer()
    for directory in roots:
        observer.schedule(Handler(), str(directory), recursive=True)
    observer.start()
    for directory in roots:
        for file in directory.rglob("*"):
            if file.is_file():
                pending.put(file)
    waiting, seen = {}, {}
    try:
        while True:
            try:
                path = pending.get(timeout=1).resolve()
                is_manifest = path.name.endswith(".paoding.json")
                is_bundled = not is_manifest and any(path.parent.glob("*.paoding.json"))
                if (
                    any(path.is_relative_to(directory) for directory in roots)
                    and not is_bundled
                    and (is_manifest or path.suffix.lower() in SUPPORTED_EXTENSIONS)
                ):
                    waiting[path] = None
            except queue.Empty:
                pass
            for path in list(waiting):
                try:
                    stat = path.stat()
                    signature = (stat.st_size, stat.st_mtime_ns)
                    if seen.get(path) == signature:
                        del waiting[path]
                        continue
                    previous = waiting[path]
                    if not previous or previous[0] != signature:
                        waiting[path] = (signature, time.monotonic())
                        continue
                    if time.monotonic() - previous[1] < 3:
                        continue
                    runtime.ingest_file(path)
                    seen[path] = signature
                    del waiting[path]
                except FileNotFoundError:
                    del waiting[path]
                except Exception as exc:
                    # Log only type; source filenames and credentials need not reach central logs.
                    logger.warning("Drop import failed: %s", type(exc).__name__)
                    del waiting[path]
    finally:
        observer.stop()
        observer.join()
