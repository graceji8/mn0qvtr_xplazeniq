import tkinter as tk
from tkinter import scrolledtext, ttk
import os
import sys

# Define log file path
LOG_FILE = os.path.join(os.getcwd(), 'logs', 'v_process.log')

def create_signal_file(filename, content='signal'):
    # Remove existing execution signal files to avoid conflict
    signals = ['/tmp/run_v_process', '/tmp/load_chrome', '/tmp/reset_cache', '/tmp/save', '/tmp/stop_automation']
    for f in signals:
        if os.path.exists(f):
            try:
                os.remove(f)
            except:
                pass
                
    with open(filename, 'w') as f:
        f.write(content)
    print(f"Created {filename} with content: {content}")

def on_run_v_process():
    project = project_entry.get().strip()
    create_signal_file('/tmp/run_v_process', content=project)
    if project:
        status_label.config(text=f"Status: Running {project}...", fg="blue")
    else:
        status_label.config(text="Status: Running Auto-Discovery...", fg="blue")

def on_stop_automation():
    with open('/tmp/stop_automation', 'w') as f:
        f.write('stop')
    status_label.config(text="Status: Stop Signal Sent!", fg="red")

def on_reload_chrome():
    create_signal_file('/tmp/load_chrome')
    status_label.config(text="Status: Reloading Chrome...", fg="orange")

def on_reset_cache():
    create_signal_file('/tmp/reset_cache')
    status_label.config(text="Status: Resetting Cache & Restarting...", fg="purple")

def on_clear_logs():
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'w') as f:
                f.truncate(0)
        
        log_display.config(state=tk.NORMAL)
        log_display.delete(1.0, tk.END)
        log_display.insert(tk.END, "--- Logs Cleared ---\n")
        log_display.config(state=tk.DISABLED)
        status_label.config(text="Status: Logs Cleared", fg="green")
    except Exception as e:
        status_label.config(text=f"Status: Clear Logs Error: {str(e)[:40]}", fg="red")

def on_save_exit():
    create_signal_file('/tmp/save')
    root.destroy()

def position_window(root, width=600, height=500):
    try:
        screen_width = root.winfo_screenwidth()
        x = screen_width - width - 50
        y = 50
        root.geometry(f'{width}x{height}+{int(x)}+{int(y)}')
    except:
        root.geometry(f'{width}x{height}+100+100')

def update_logs():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, 'r') as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 10000), os.SEEK_SET)
                content = f.read()
                
                log_display.config(state=tk.NORMAL)
                log_display.delete(1.0, tk.END)
                log_display.insert(tk.END, "...(showing last ~100 lines)...\n")
                log_display.insert(tk.END, content)
                log_display.see(tk.END)
                log_display.config(state=tk.DISABLED)
        except Exception as e:
            pass
    root.after(1000, update_logs)

root = tk.Tk()
root.title("V-Process Automation Control Panel")
position_window(root, width=600, height=550)
root.attributes('-topmost', True)

label = tk.Label(root, text="V-Process Control Center", font=("Arial", 14, "bold"))
label.pack(pady=10)

status_label = tk.Label(root, text="Status: Idle", font=("Arial", 10), fg="gray")
status_label.pack(pady=5)

project_frame = tk.Frame(root)
project_frame.pack(pady=5)

tk.Label(project_frame, text="Project: ", font=("Arial", 10)).pack(side=tk.LEFT)
project_entry = tk.Entry(project_frame, font=("Arial", 10), width=30)
project_entry.insert(0, "2026-09-20-project")
project_entry.pack(side=tk.LEFT, padx=5)

btn_frame1 = tk.Frame(root)
btn_frame1.pack(pady=10)

btn_run = tk.Button(btn_frame1, text="🚀 Run Project", command=on_run_v_process, bg="#4CAF50", fg="white", font=("Arial", 11, "bold"), width=18, height=2)
btn_run.pack(side=tk.LEFT, padx=5)

btn_auto = tk.Button(btn_frame1, text="🔍 Auto-Discover", command=lambda: (project_entry.delete(0, tk.END), on_run_v_process()), bg="#2196F3", fg="white", font=("Arial", 11, "bold"), width=18, height=2)
btn_auto.pack(side=tk.LEFT, padx=5)

btn_stop = tk.Button(btn_frame1, text="🛑 Stop Action", command=on_stop_automation, bg="#f44336", fg="white", font=("Arial", 11, "bold"), width=18, height=2)
btn_stop.pack(side=tk.LEFT, padx=5)

btn_frame2 = tk.Frame(root)
btn_frame2.pack(pady=5)

btn_reload = tk.Button(btn_frame2, text="🔄 Reload Chrome", command=on_reload_chrome, bg="#FF9800", fg="white", font=("Arial", 10), width=18)
btn_reload.pack(side=tk.LEFT, padx=5)

btn_reset = tk.Button(btn_frame2, text="🧹 Clear Cache", command=on_reset_cache, bg="#795548", fg="white", font=("Arial", 10), width=18)
btn_reset.pack(side=tk.LEFT, padx=5)

btn_frame3 = tk.Frame(root)
btn_frame3.pack(pady=10)

btn_save = tk.Button(btn_frame3, text="⏹️ SAVE & EXIT RUNNER", command=on_save_exit, bg="#d32f2f", fg="white", font=("Arial", 12, "bold"), width=30, height=2)
btn_save.pack(padx=5)

tk.Label(root, text="Runner will wait for projects until EXIT is clicked.", font=("Arial", 9, "italic"), fg="gray").pack(pady=2)

log_frame = tk.LabelFrame(root, text="Live Logs (logs/v_process.log)", font=("Arial", 10))
log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

log_top_bar = tk.Frame(log_frame)
log_top_bar.pack(fill=tk.X, padx=5, pady=2)

btn_clear = tk.Button(log_top_bar, text="Clear Logs", command=on_clear_logs, bg="#9E9E9E", fg="white", font=("Arial", 9), width=10)
btn_clear.pack(side=tk.RIGHT)

log_display = scrolledtext.ScrolledText(log_frame, state=tk.DISABLED, height=8, font=("Consolas", 9))
log_display.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

root.after(2000, lambda: root.attributes('-topmost', False))
root.after(4000, lambda: root.attributes('-topmost', True))
update_logs()
root.mainloop()
