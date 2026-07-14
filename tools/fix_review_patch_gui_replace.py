#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from pathlib import Path

script = Path(__file__).resolve().with_name("apply_review_fixes.py")
text = script.read_text(encoding="utf-8")

needle = "app = one(app, '''    def log(self, message):"
start = text.find(needle)
if start < 0:
    if "app = between(app, \"    def log(self, message):\\n\"" in text:
        print("GUI replacement already robust")
        raise SystemExit(0)
    raise RuntimeError("GUI UI replacement block start not found")

end_marker = "\n\napp = one(app, \"        save_config()\\n        if config[\\\"email_provider\\\"] == \\\"cloudflare\\\""
end = text.find(end_marker, start)
if end < 0:
    raise RuntimeError("GUI UI replacement block end not found")

replacement = r'''app = between(app, "    def log(self, message):\n", "    def should_stop(self):\n", ''' + "'''" + r'''    def _call_ui(self, func, *args):
        if threading.get_ident() == self._ui_thread_id:
            func(*args)
            return
        try:
            self.root.after(0, lambda: func(*args))
        except Exception:
            pass

    def _append_log_line(self, line):
        self.log_text.insert(tk.END, f"{line}\\n")
        self.log_text.see(tk.END)

    def log(self, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        print(line, flush=True)
        self._call_ui(self._append_log_line, line)

    def clear_log(self):
        self._call_ui(lambda: self.log_text.delete(1.0, tk.END))

    def update_stats(self):
        self._call_ui(lambda: self.stats_var.set(f"成功: {self.success_count} | 失败: {self.fail_count}"))

    def _set_running_ui(self, running):
        self.is_running = running
        def apply():
            self.start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
            self.stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)
            self.status_var.set("运行中..." if running else "就绪")
            self.status_label.config(foreground="blue" if running else "green")
        self._call_ui(apply)


''' + "'''" + r''', "GUI UI calls")
'''

text = text[:start] + replacement + text[end:]
script.write_text(text, encoding="utf-8")
print("review patch GUI replacement made robust")
